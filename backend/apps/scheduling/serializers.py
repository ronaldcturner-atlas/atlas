from datetime import datetime, timedelta

from django.db import models
from rest_framework import serializers
from django.utils import timezone

from apps.accounts.models import Physician
from apps.facilities.models import Facility

from .models import (
    OptimizerRun,
    ScheduleBlock,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    Shift,
    ShiftTemplate,
)
from .models import Contract, ContractUserAssignment


class ShiftSerializer(serializers.ModelSerializer):
    facility_name = serializers.CharField(source='facility.name', read_only=True)
    physician_name = serializers.SerializerMethodField()
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    shift_type_display = serializers.CharField(source='get_shift_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    start_datetime = serializers.SerializerMethodField()
    end_datetime = serializers.SerializerMethodField()

    class Meta:
        model = Shift
        fields = [
            'id',
            'facility',
            'facility_name',
            'physician',
            'physician_name',
            'role',
            'role_display',
            'date',
            'start_time',
            'end_time',
            'shift_type',
            'shift_type_display',
            'status',
            'status_display',
            'notes',
            'start_datetime',
            'end_datetime',
        ]
        read_only_fields = [
            'id',
            'facility_name',
            'physician_name',
            'role_display',
            'shift_type_display',
            'status_display',
            'start_datetime',
            'end_datetime',
        ]

    def get_physician_name(self, obj):
        return obj.physician.display_name or obj.physician.user.get_full_name() or obj.physician.user.username

    def get_start_datetime(self, obj):
        return datetime.combine(obj.date, obj.start_time).isoformat()

    def get_end_datetime(self, obj):
        end_date = obj.date
        if obj.end_time <= obj.start_time:
            end_date = end_date + timedelta(days=1)
        return datetime.combine(end_date, obj.end_time).isoformat()

    def validate(self, attrs):
        start_time = attrs.get('start_time', getattr(self.instance, 'start_time', None))
        end_time = attrs.get('end_time', getattr(self.instance, 'end_time', None))

        if start_time and end_time and start_time == end_time:
            raise serializers.ValidationError({'end_time': 'End time must be different from start time.'})

        return attrs


class ShiftTemplateSerializer(serializers.ModelSerializer):
    facility_name = serializers.CharField(source='facility.name', read_only=True)
    name = serializers.SerializerMethodField()

    class Meta:
        model = ShiftTemplate
        fields = [
            'id',
            'facility',
            'facility_name',
            'name',
            'start_time',
            'end_time',
            'active_days_of_week',
            'weekend_days',
            'night_shift',
            'default_staffing_count',
            'active',
        ]
        read_only_fields = ['id', 'facility_name', 'name']

    def _format_template_time(self, time_value):
        hour_24 = time_value.hour
        minute = time_value.minute
        suffix = 'a' if hour_24 < 12 else 'p'
        hour_12 = hour_24 % 12 or 12

        if minute == 0:
            return f'{hour_12}{suffix}'

        return f'{hour_12}:{minute:02d}{suffix}'

    def _build_generated_name(self, facility, start_time, end_time):
        return f'{facility.short_name} {self._format_template_time(start_time)}-{self._format_template_time(end_time)}'

    def get_name(self, obj):
        return self._build_generated_name(obj.facility, obj.start_time, obj.end_time)

    def validate_active_days_of_week(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Active days must be an array of day names.')

        allowed = set(ShiftTemplate.DAYS_OF_WEEK)
        normalized = []
        seen = set()

        for day in value:
            if not isinstance(day, str) or day not in allowed:
                raise serializers.ValidationError(
                    f'Invalid day "{day}". Allowed values: {", ".join(ShiftTemplate.DAYS_OF_WEEK)}.'
                )
            if day in seen:
                continue
            seen.add(day)
            normalized.append(day)

        if not normalized:
            raise serializers.ValidationError('Select at least one active day.')

        return normalized

    def validate_weekend_days(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Weekend days must be an array of day names.')

        allowed = set(ShiftTemplate.WEEKEND_ALLOWED_DAYS)
        normalized = []
        seen = set()

        for day in value:
            if not isinstance(day, str) or day not in allowed:
                raise serializers.ValidationError(
                    f'Invalid weekend day "{day}". Allowed values: {", ".join(ShiftTemplate.WEEKEND_ALLOWED_DAYS)}.'
                )
            if day in seen:
                continue
            seen.add(day)
            normalized.append(day)

        return normalized

    def validate_default_staffing_count(self, value):
        if value < 1:
            raise serializers.ValidationError('Required staffing must be at least 1.')
        return value

    def create(self, validated_data):
        validated_data['name'] = self._build_generated_name(
            validated_data['facility'],
            validated_data['start_time'],
            validated_data['end_time'],
        )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        facility = validated_data.get('facility', instance.facility)
        start_time = validated_data.get('start_time', instance.start_time)
        end_time = validated_data.get('end_time', instance.end_time)
        validated_data['name'] = self._build_generated_name(facility, start_time, end_time)
        return super().update(instance, validated_data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        active_days = attrs.get('active_days_of_week', getattr(self.instance, 'active_days_of_week', []))
        weekend_days = attrs.get('weekend_days', getattr(self.instance, 'weekend_days', []))

        invalid_weekend_days = [day for day in weekend_days if day not in active_days]
        if invalid_weekend_days:
            raise serializers.ValidationError({
                'weekend_days': 'Weekend designation days must also be selected in active days.'
            })

        return attrs


class ScheduleBlockSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    request_status = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleBlock
        fields = [
            'id',
            'name',
            'start_date',
            'end_date',
            'request_open_datetime',
            'request_close_datetime',
            'request_status',
            'build_status',
            'created_at',
            'updated_at',
            'published_at',
        ]
        read_only_fields = [
            'id',
            'name',
            'request_status',
            'build_status',
            'created_at',
            'updated_at',
            'published_at',
        ]

    def get_name(self, obj):
        return obj.generated_name

    def get_request_status(self, obj):
        now = timezone.now()
        if now < obj.request_open_datetime:
            return 'Not Open'
        if now <= obj.request_close_datetime:
            return 'Open'
        return 'Closed'

    def validate(self, attrs):
        attrs = super().validate(attrs)

        if 'build_status' in self.initial_data:
            raise serializers.ValidationError({'build_status': 'build_status cannot be edited manually.'})

        instance = self.instance
        start_date = attrs.get('start_date', getattr(instance, 'start_date', None))
        end_date = attrs.get('end_date', getattr(instance, 'end_date', None))
        request_open_datetime = attrs.get(
            'request_open_datetime',
            getattr(instance, 'request_open_datetime', None),
        )
        request_close_datetime = attrs.get(
            'request_close_datetime',
            getattr(instance, 'request_close_datetime', None),
        )

        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({'end_date': 'End date must be on or after start date.'})

        if start_date and end_date:
            month_span = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
            if month_span < 1:
                raise serializers.ValidationError({'end_date': 'Schedule block must be at least 1 month.'})
            if month_span > 12:
                raise serializers.ValidationError({'end_date': 'Schedule block cannot exceed 12 months.'})

        if request_open_datetime and request_close_datetime and request_close_datetime <= request_open_datetime:
            raise serializers.ValidationError({
                'request_close_datetime': 'Request close must be later than request open.'
            })

        if instance and instance.build_status == ScheduleBlock.BuildStatus.ARCHIVE:
            editable_fields = {
                'start_date',
                'end_date',
                'request_open_datetime',
                'request_close_datetime',
            }
            attempted_edits = editable_fields.intersection(set(attrs.keys()))
            if attempted_edits:
                raise serializers.ValidationError('Archived Schedule Blocks are read only.')

        return attrs


class ScheduleVersionSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    shift_instance_count = serializers.SerializerMethodField()
    active_optimizer_run = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleVersion
        fields = [
            'id',
            'schedule_block',
            'domain',
            'domain_name',
            'version_number',
            'name',
            'status',
            'optimizer_summary',
            'score_is_stale',
            'active_optimizer_run',
            'shift_instance_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_shift_instance_count(self, obj):
        return obj.shift_instances.filter(
            date__gte=obj.schedule_block.start_date,
            date__lte=obj.schedule_block.end_date,
        ).count()

    def get_active_optimizer_run(self, obj):
        run = getattr(obj, 'active_optimizer_run_cached', None)
        if run is None:
            run = obj.optimizer_runs.filter(is_active=True).order_by('-run_number').first()
        return OptimizerRunSerializer(run).data if run else None


class OptimizerRunSerializer(serializers.ModelSerializer):
    schedule_version_name = serializers.CharField(source='schedule_version.name', read_only=True)
    copied_from_run_number = serializers.IntegerField(source='copied_from_run.run_number', read_only=True)

    class Meta:
        model = OptimizerRun
        fields = [
            'id',
            'schedule_version',
            'schedule_version_name',
            'run_number',
            'created_at',
            'created_by',
            'status',
            'seed',
            'initial_score',
            'final_score',
            'score_breakdown',
            'optimizer_summary',
            'optimizer_debug',
            'notes',
            'is_active',
            'score_is_stale',
            'copied_from_run',
            'copied_from_run_number',
            'run_kind',
            'locked_open_shift_instance_ids',
            'start_mode',
        ]
        read_only_fields = fields


class ScheduleShiftAssignmentSerializer(serializers.ModelSerializer):
    physician_name = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleShiftAssignment
        fields = [
            'id',
            'shift_instance',
            'physician',
            'physician_name',
            'assignment_source',
            'is_locked',
            'created_by',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_physician_name(self, obj):
        return (
            obj.physician.display_name
            or obj.physician.user.get_full_name()
            or obj.physician.user.username
        )


class ScheduleShiftInstanceSerializer(serializers.ModelSerializer):
    facility_name = serializers.CharField(source='facility.name', read_only=True)
    facility_short_name = serializers.CharField(source='facility.short_name', read_only=True)
    shift_template_name = serializers.SerializerMethodField()
    template_start_time = serializers.TimeField(source='shift_template.start_time', read_only=True)
    template_end_time = serializers.TimeField(source='shift_template.end_time', read_only=True)
    assigned_count = serializers.SerializerMethodField()
    open_count = serializers.SerializerMethodField()
    is_open = serializers.SerializerMethodField()
    assignments = serializers.SerializerMethodField()
    is_locked_open = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleShiftInstance
        fields = [
            'id',
            'schedule_version',
            'schedule_block',
            'date',
            'shift_template',
            'shift_template_name',
            'facility',
            'facility_name',
            'facility_short_name',
            'start_datetime',
            'end_datetime',
            'template_start_time',
            'template_end_time',
            'required_staffing',
            'assignments',
            'assigned_count',
            'open_count',
            'is_open',
            'status',
            'is_locked_open',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_shift_template_name(self, obj):
        return obj.shift_template.generated_name()

    def get_assigned_count(self, obj):
        return self._visible_assignments(obj).count()

    def get_open_count(self, obj):
        return max(obj.required_staffing - self.get_assigned_count(obj), 0)

    def get_is_open(self, obj):
        return self.get_open_count(obj) > 0

    def get_assignments(self, obj):
        return ScheduleShiftAssignmentSerializer(self._visible_assignments(obj), many=True).data

    def get_is_locked_open(self, obj):
        optimizer_run_id = self.context.get('optimizer_run_id')
        if optimizer_run_id:
            locked_ids = self.context.get('locked_open_shift_instance_ids')
            if locked_ids is None:
                locked_ids = OptimizerRun.objects.filter(id=optimizer_run_id).values_list(
                    'locked_open_shift_instance_ids', flat=True
                ).first() or []
                self.context['locked_open_shift_instance_ids'] = locked_ids
            return obj.id in locked_ids
        return obj.is_locked_open

    def _visible_assignments(self, obj):
        optimizer_run_id = self.context.get('optimizer_run_id')
        query = obj.assignments.select_related('physician__user')
        if optimizer_run_id:
            run_kind = self.context.get('optimizer_run_kind')
            if run_kind is None:
                run_kind = OptimizerRun.objects.filter(id=optimizer_run_id).values_list('run_kind', flat=True).first()
                self.context['optimizer_run_kind'] = run_kind
            if run_kind == 'COPY':
                return query.filter(optimizer_run_id=optimizer_run_id)
            return query.filter(
                models.Q(assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL, optimizer_run__isnull=True)
                | models.Q(optimizer_run_id=optimizer_run_id)
            )
        return query.filter(assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL, optimizer_run__isnull=True)


class ScheduleRequestSerializer(serializers.ModelSerializer):
    physician_name = serializers.SerializerMethodField()
    shift_template_ids = serializers.PrimaryKeyRelatedField(
        source='shift_templates',
        many=True,
        read_only=True,
    )
    shift_template_details = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleRequest
        fields = [
            'id',
            'schedule_block',
            'physician',
            'physician_name',
            'date',
            'request_scope',
            'request_type',
            'weight',
            'shift_template_ids',
            'shift_template_details',
            'created_by',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_physician_name(self, obj):
        return obj.physician.display_name or obj.physician.user.get_full_name() or obj.physician.user.username

    def get_shift_template_details(self, obj):
        return [
            {
                'id': template.id,
                'name': template.generated_name(),
                'facility_name': template.facility.name,
            }
            for template in obj.shift_templates.all()
        ]


class ContractSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    facility_ids = serializers.PrimaryKeyRelatedField(
        source='facilities',
        many=True,
        queryset=Facility.objects.all(),
        required=False,
    )
    assigned_user_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        write_only=True,
        required=False,
    )
    assigned_users = serializers.SerializerMethodField()
    assigned_users_count = serializers.SerializerMethodField()

    class Meta:
        model = Contract
        fields = [
            'id',
            'domain',
            'domain_name',
            'name',
            'active',
            'facility_ids',
            'workload_settings',
            'shift_settings',
            'night_settings',
            'weekend_settings',
            'request_settings',
            'assigned_user_ids',
            'assigned_users',
            'assigned_users_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'domain_name',
            'assigned_users',
            'assigned_users_count',
            'created_at',
            'updated_at',
        ]

    def get_assigned_users(self, obj):
        assignments = obj.user_assignments.select_related('physician__user').all()
        return [
            {
                'id': assignment.physician_id,
                'name': assignment.physician.display_name
                or assignment.physician.user.get_full_name()
                or assignment.physician.user.username,
            }
            for assignment in assignments
        ]

    def get_assigned_users_count(self, obj):
        return obj.user_assignments.count()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Name is required.')
        return value

    def validate_assigned_user_ids(self, value):
        unique_ids = sorted(set(value))
        matched_count = Physician.objects.filter(id__in=unique_ids).count()
        if matched_count != len(unique_ids):
            raise serializers.ValidationError('One or more physicians do not exist.')
        return unique_ids

    def validate(self, attrs):
        attrs = super().validate(attrs)

        assigned_user_ids = attrs.get('assigned_user_ids')
        next_active = attrs.get('active', self.instance.active if self.instance else True)

        if assigned_user_ids and not next_active:
            raise serializers.ValidationError({
                'assigned_user_ids': 'Inactive contracts cannot be assigned to users unless reactivated.'
            })

        return attrs

    def _save_assignments(self, contract, assigned_user_ids):
        if assigned_user_ids is None:
            return

        ContractUserAssignment.objects.filter(contract=contract).exclude(physician_id__in=assigned_user_ids).delete()

        existing_ids = set(
            ContractUserAssignment.objects.filter(contract=contract).values_list('physician_id', flat=True)
        )

        for physician_id in assigned_user_ids:
            if physician_id in existing_ids:
                continue

            # Replace any previous default contract in this domain for this physician.
            ContractUserAssignment.objects.filter(
                domain=contract.domain,
                physician_id=physician_id,
            ).exclude(contract=contract).delete()

            ContractUserAssignment.objects.create(
                contract=contract,
                domain=contract.domain,
                physician_id=physician_id,
            )

    def create(self, validated_data):
        assigned_user_ids = validated_data.pop('assigned_user_ids', None)
        facilities = validated_data.pop('facilities', [])

        contract = Contract.objects.create(**validated_data)
        contract.facilities.set(facilities)
        self._save_assignments(contract, assigned_user_ids)
        return contract

    def update(self, instance, validated_data):
        assigned_user_ids = validated_data.pop('assigned_user_ids', None)
        facilities = validated_data.pop('facilities', None)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()

        if facilities is not None:
            instance.facilities.set(facilities)

        self._save_assignments(instance, assigned_user_ids)
        return instance
