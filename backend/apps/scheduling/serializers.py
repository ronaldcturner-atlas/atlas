from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import models
from rest_framework import serializers
from django.utils import timezone

from apps.accounts.models import Physician
from apps.domains.models import Domain, DomainMembership
from apps.facilities.models import Facility

from .models import (
    OptimizerControl,
    OptimizerRun,
    ScheduleBlock,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    Shift,
    ShiftTemplate,
)
from .models import (
    Contract, ContractUserAssignment, SharedRule, SharedRuleContract,
)
from .shared_rules import sync_shared_rule_contract_settings


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
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    region = serializers.IntegerField(source='domain.region_id', read_only=True)
    region_name = serializers.CharField(source='domain.region.name', read_only=True)
    organization = serializers.IntegerField(source='domain.region.organization_id', read_only=True)
    organization_name = serializers.CharField(source='domain.region.organization.name', read_only=True)
    facility_name = serializers.CharField(source='facility.name', read_only=True)
    facility_sort_order = serializers.IntegerField(source='facility.sort_order', read_only=True)
    name = serializers.SerializerMethodField()

    class Meta:
        model = ShiftTemplate
        fields = [
            'id',
            'domain',
            'domain_name',
            'region',
            'region_name',
            'organization',
            'organization_name',
            'facility',
            'facility_name',
            'facility_sort_order',
            'name',
            'start_time',
            'end_time',
            'active_days_of_week',
            'weekend_days',
            'night_shift',
            'default_staffing_count',
            'active',
        ]
        read_only_fields = ['id', 'domain_name', 'region', 'region_name', 'organization', 'organization_name', 'facility_name', 'facility_sort_order', 'name']

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
        domain = attrs.get('domain', self.instance.domain if self.instance else None)
        facility = attrs.get('facility', self.instance.facility if self.instance else None)
        if domain and facility and domain.region_id != facility.region_id:
            raise serializers.ValidationError({
                'facility': 'Facility must belong to the same Region as the selected Domain.',
            })
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
    published_runs = serializers.SerializerMethodField()
    domain = serializers.PrimaryKeyRelatedField(queryset=Domain.objects.filter(active=True))
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    region = serializers.IntegerField(source='domain.region_id', read_only=True)
    region_name = serializers.CharField(source='domain.region.name', read_only=True)
    preview_run_number = serializers.IntegerField(
        source='preview_optimizer_run.run_number', read_only=True, allow_null=True,
    )

    class Meta:
        model = ScheduleBlock
        fields = [
            'id',
            'name',
            'domain',
            'domain_name',
            'region',
            'region_name',
            'start_date',
            'end_date',
            'request_open_datetime',
            'request_close_datetime',
            'request_status',
            'build_status',
            'created_at',
            'updated_at',
            'published_at',
            'published_runs',
            'preview_optimizer_run',
            'preview_run_number',
        ]
        read_only_fields = [
            'id',
            'name',
            'domain_name',
            'region',
            'region_name',
            'request_status',
            'build_status',
            'created_at',
            'updated_at',
            'published_at',
            'published_runs',
            'preview_optimizer_run',
            'preview_run_number',
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

    def get_published_runs(self, obj):
        if obj.published_at is None:
            return []
        return [
            {
                'schedule_version_id': version.id,
                'domain_name': version.domain.name,
                'run_id': version.published_optimizer_run_id,
                'run_number': (
                    version.published_optimizer_run.run_number
                    if version.published_optimizer_run is not None
                    else None
                ),
            }
            for version in obj.schedule_versions.filter(domain=obj.domain).select_related(
                'domain', 'published_optimizer_run',
            ).order_by('id')
        ]

    def validate(self, attrs):
        attrs = super().validate(attrs)

        if 'build_status' in self.initial_data:
            raise serializers.ValidationError({'build_status': 'build_status cannot be edited manually.'})

        if self.instance is not None and 'domain' in attrs and attrs['domain'].id != self.instance.domain_id:
            raise serializers.ValidationError({
                'domain': 'A Schedule Block cannot be moved to another Domain.',
            })

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

        if instance and instance.build_status in {
            ScheduleBlock.BuildStatus.PREVIEW,
            ScheduleBlock.BuildStatus.ARCHIVE,
        }:
            editable_fields = {
                'start_date',
                'end_date',
                'request_open_datetime',
                'request_close_datetime',
            }
            attempted_edits = editable_fields.intersection(set(attrs.keys()))
            if attempted_edits:
                phase = instance.get_build_status_display()
                raise serializers.ValidationError(f'{phase} Schedule Blocks are read only.')

        return attrs


class ScheduleVersionSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    shift_instance_count = serializers.SerializerMethodField()
    active_optimizer_run = serializers.SerializerMethodField()
    published_optimizer_run = serializers.SerializerMethodField()

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
            'workload_hour_overrides',
            'score_is_stale',
            'active_optimizer_run',
            'published_optimizer_run',
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

    def get_published_optimizer_run(self, obj):
        run = obj.published_optimizer_run
        if run is None:
            return None
        return {
            'id': run.id,
            'run_number': run.run_number,
            'final_score': run.final_score,
        }


class ScheduleVersionWorkspaceSerializer(serializers.ModelSerializer):
    """Lightweight version metadata for the Build Workspace header and selector."""
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    shift_instance_count = serializers.SerializerMethodField()
    published_optimizer_run = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleVersion
        fields = [
            'id', 'schedule_block', 'domain', 'domain_name', 'version_number',
            'name', 'status', 'score_is_stale', 'shift_instance_count',
            'published_optimizer_run',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_shift_instance_count(self, obj):
        return obj.shift_instances.filter(
            date__gte=obj.schedule_block.start_date,
            date__lte=obj.schedule_block.end_date,
        ).count()

    def get_published_optimizer_run(self, obj):
        run = obj.published_optimizer_run
        if run is None:
            return None
        return {
            'id': run.id,
            'run_number': run.run_number,
            'final_score': run.final_score,
        }


class OptimizerRunSerializer(serializers.ModelSerializer):
    schedule_version_name = serializers.CharField(source='schedule_version.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    copied_from_run_number = serializers.IntegerField(source='copied_from_run.run_number', read_only=True)
    started_at = serializers.SerializerMethodField()
    live_best_score = serializers.SerializerMethodField()
    is_published = serializers.SerializerMethodField()

    def get_started_at(self, obj):
        try:
            control = obj.control
        except OptimizerControl.DoesNotExist:
            return None
        return control.started_at

    def get_live_best_score(self, obj):
        try:
            control = obj.control
        except OptimizerControl.DoesNotExist:
            return None
        return control.live_best_score

    def get_is_published(self, obj):
        return obj.schedule_version.published_optimizer_run_id == obj.id

    def get_created_by_name(self, obj):
        if obj.created_by is None:
            return None
        return (
            obj.created_by.get_full_name().strip()
            or obj.created_by.email
            or obj.created_by.username
        )

    class Meta:
        model = OptimizerRun
        fields = [
            'id',
            'schedule_version',
            'schedule_version_name',
            'run_number',
            'created_at',
            'started_at',
            'live_best_score',
            'created_by',
            'created_by_name',
            'status',
            'seed',
            'initial_score',
            'final_score',
            'score_breakdown',
            'optimizer_summary',
            'optimizer_debug',
            'notes',
            'is_active',
            'is_published',
            'score_is_stale',
            'copied_from_run',
            'copied_from_run_number',
            'started_from_run',
            'started_from_run_number',
            'run_kind',
            'locked_open_shift_instance_ids',
            'start_mode',
            'max_runtime_seconds',
            'optimization_focus',
        ]
        read_only_fields = fields


class OptimizerRunHistorySerializer(serializers.ModelSerializer):
    """Compact run metadata for workspace selection controls."""
    copied_from_run_number = serializers.IntegerField(
        source='copied_from_run.run_number', read_only=True,
    )
    runtime_seconds = serializers.SerializerMethodField()
    started_at = serializers.SerializerMethodField()
    live_best_score = serializers.SerializerMethodField()
    is_published = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    def get_started_at(self, obj):
        try:
            control = obj.control
        except OptimizerControl.DoesNotExist:
            return None
        return control.started_at

    def get_live_best_score(self, obj):
        try:
            control = obj.control
        except OptimizerControl.DoesNotExist:
            return None
        return control.live_best_score

    def get_is_published(self, obj):
        return obj.schedule_version.published_optimizer_run_id == obj.id

    def get_created_by_name(self, obj):
        if obj.created_by is None:
            return None
        return (
            obj.created_by.get_full_name().strip()
            or obj.created_by.email
            or obj.created_by.username
        )

    def get_runtime_seconds(self, obj):
        annotated_runtime = getattr(obj, 'runtime_seconds_value', None)
        if annotated_runtime is not None:
            return float(annotated_runtime)
        value = (obj.optimizer_summary or {}).get('runtime_seconds')
        return float(value) if value is not None else None

    class Meta:
        model = OptimizerRun
        fields = [
            'id', 'schedule_version', 'run_number', 'created_at', 'started_at',
            'live_best_score', 'created_by_name', 'status', 'seed',
            'initial_score', 'final_score', 'is_active', 'is_published', 'score_is_stale',
            'copied_from_run', 'copied_from_run_number', 'run_kind',
            'started_from_run', 'started_from_run_number',
            'locked_open_shift_instance_ids', 'start_mode',
            'max_runtime_seconds',
            'optimization_focus',
            'runtime_seconds',
            'notes',
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
        return len(self._visible_assignments(obj))

    def get_open_count(self, obj):
        return max(obj.required_staffing - self.get_assigned_count(obj), 0)

    def get_is_open(self, obj):
        return self.get_open_count(obj) > 0

    def get_assignments(self, obj):
        return ScheduleShiftAssignmentSerializer(self._visible_assignments(obj), many=True).data

    def get_is_locked_open(self, obj):
        from .run_state import locked_open_ids
        optimizer_run_id = self.context.get('optimizer_run_id')
        if optimizer_run_id:
            viewed_run = self.context.get('viewed_run')
            if viewed_run is None:
                viewed_run = OptimizerRun.objects.filter(id=optimizer_run_id).first()
                self.context['viewed_run'] = viewed_run
            return obj.id in locked_open_ids(viewed_run)
        return obj.is_locked_open

    def _visible_assignments(self, obj):
        cached = getattr(obj, 'visible_assignments_cached', None)
        if cached is not None:
            return cached
        from .run_state import visible_assignment_filter
        optimizer_run_id = self.context.get('optimizer_run_id')
        query = obj.assignments.select_related('physician__user')
        if optimizer_run_id:
            viewed_run = self.context.get('viewed_run')
            if viewed_run is None:
                viewed_run = OptimizerRun.objects.filter(id=optimizer_run_id).first()
                self.context['viewed_run'] = viewed_run
            return list(query.filter(visible_assignment_filter(viewed_run)))
        return list(query.filter(visible_assignment_filter(None)))


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
        templates = sorted(
            obj.shift_templates.all(),
            key=lambda template: (
                template.facility.sort_order,
                template.facility.name,
                template.start_time,
                template.end_time,
                template.id,
            ),
        )
        return [
            {
                'id': template.id,
                'name': template.generated_name(),
                'facility_name': template.facility.name,
            }
            for template in templates
        ]


class ContractSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    region = serializers.IntegerField(source='domain.region_id', read_only=True)
    region_name = serializers.CharField(source='domain.region.name', read_only=True)
    organization = serializers.IntegerField(source='domain.region.organization_id', read_only=True)
    organization_name = serializers.CharField(source='domain.region.organization.name', read_only=True)
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
    shared_rules = serializers.SerializerMethodField()
    setup_required = serializers.SerializerMethodField()
    shared_rule_settings = serializers.ListField(
        child=serializers.DictField(), write_only=True, required=False,
    )

    class Meta:
        model = Contract
        fields = [
            'id',
            'domain',
            'domain_name',
            'region',
            'region_name',
            'organization',
            'organization_name',
            'name',
            'active',
            'manual_assignment_only',
            'facility_ids',
            'workload_settings',
            'shift_settings',
            'night_settings',
            'weekend_settings',
            'request_settings',
            'assigned_user_ids',
            'assigned_users',
            'assigned_users_count',
            'shared_rules',
            'setup_required',
            'shared_rule_settings',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'domain_name',
            'region',
            'region_name',
            'organization',
            'organization_name',
            'assigned_users',
            'assigned_users_count',
            'shared_rules',
            'setup_required',
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

    def get_setup_required(self, obj):
        return not obj.facilities.exists()

    def get_shared_rules(self, obj):
        facility_ids = set(obj.facilities.values_list('id', flat=True))
        rows = []
        for link in obj.shared_rule_links.select_related(
            'shared_rule',
        ).prefetch_related('shared_rule__shift_templates__facility'):
            rule = link.shared_rule
            templates = [
                {
                    'id': template.id,
                    'name': template.generated_name(),
                    'facility_id': template.facility_id,
                    'facility_name': template.facility.name,
                }
                for template in rule.shift_templates.all()
                if template.facility_id in facility_ids
            ]
            rows.append({
                'id': rule.id,
                'name': rule.name,
                'active': rule.active,
                'enabled': link.enabled,
                'period_type': rule.period_type,
                'units': rule.units,
                'shift_templates': templates,
                'min_value': (
                    str(int(link.min_value)) if link.min_value is not None else ''
                ),
                'max_value': (
                    str(int(link.max_value)) if link.max_value is not None else ''
                ),
                'min_penalty_weight': (
                    str(int(link.min_penalty_weight))
                    if link.min_penalty_weight is not None else ''
                ),
                'max_penalty_weight': (
                    str(int(link.max_penalty_weight))
                    if link.max_penalty_weight is not None else ''
                ),
                'spread_violations': link.spread_violations,
            })
        return rows

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

    @staticmethod
    def _normalize_whole_number_fields(row, fields, label):
        for field in fields:
            value = row.get(field)
            if value in (None, ''):
                continue
            try:
                decimal_value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                raise serializers.ValidationError({
                    label: f'{field.replace("_", " ")} must be a whole number.',
                })
            if (
                decimal_value != decimal_value.to_integral_value()
                or decimal_value < 0
            ):
                raise serializers.ValidationError({
                    label: f'{field.replace("_", " ")} must be a nonnegative whole number.',
                })
            row[field] = str(int(decimal_value))

    def _normalize_contract_rule_numbers(self, attrs):
        period_field_sets = {
            'workload_settings': (
                'min_value', 'max_value',
                'min_penalty_weight', 'max_penalty_weight',
            ),
            'night_settings': (
                'min_shifts', 'max_shifts',
                'min_penalty_weight', 'max_penalty_weight',
            ),
            'weekend_settings': (
                'min_volume', 'max_volume',
                'min_penalty_weight', 'max_penalty_weight',
            ),
        }
        for settings_name, fields in period_field_sets.items():
            settings = attrs.get(settings_name)
            if not isinstance(settings, dict):
                continue
            for row in settings.get('period_rules') or []:
                if isinstance(row, dict):
                    self._normalize_whole_number_fields(
                        row, fields, settings_name,
                    )

        shift_settings = attrs.get('shift_settings')
        if isinstance(shift_settings, dict):
            for group in shift_settings.get('rules') or []:
                if not isinstance(group, dict):
                    continue
                for row in group.get('period_rules') or []:
                    if isinstance(row, dict):
                        self._normalize_whole_number_fields(
                            row,
                            (
                                'min_value', 'max_value',
                                'min_penalty_weight', 'max_penalty_weight',
                            ),
                            'shift_settings',
                        )

        scalar_fields = {
            'workload_settings': (
                'min_time_off_hours', 'min_time_off_penalty_weight',
                'circadian_penalty_weight',
                'min_days_in_row', 'min_days_in_row_penalty_weight',
                'max_days_in_row', 'max_days_in_row_penalty_weight',
                'min_same_shifts_in_row',
                'min_same_shifts_in_row_penalty_weight',
                'max_same_shifts_in_row',
                'max_same_shifts_in_row_penalty_weight',
            ),
            'night_settings': (
                'min_consecutive_night_shifts',
                'min_consecutive_night_shifts_penalty_weight',
                'max_consecutive_night_shifts',
                'max_consecutive_night_shifts_penalty_weight',
                'days_off_after_night_block',
                'days_off_after_night_block_penalty_weight',
                'days_off_before_next_night_shift',
                'days_off_before_next_night_shift_penalty_weight',
            ),
            'weekend_settings': (
                'min_consecutive_weekends',
                'min_consecutive_weekends_penalty_weight',
                'max_consecutive_weekends',
                'max_consecutive_weekends_penalty_weight',
                'min_consecutive_weekend_shifts',
                'min_consecutive_weekend_shifts_penalty_weight',
                'max_consecutive_weekend_shifts',
                'max_consecutive_weekend_shifts_penalty_weight',
                'block_friday_night_before_weekend_off_penalty_weight',
            ),
        }
        for settings_name, fields in scalar_fields.items():
            settings = attrs.get(settings_name)
            if isinstance(settings, dict):
                self._normalize_whole_number_fields(
                    settings, fields, settings_name,
                )
        return attrs

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs = self._normalize_contract_rule_numbers(attrs)

        assigned_user_ids = attrs.get('assigned_user_ids')
        next_active = attrs.get('active', self.instance.active if self.instance else True)

        if assigned_user_ids and not next_active:
            raise serializers.ValidationError({
                'assigned_user_ids': 'Inactive contracts cannot be assigned to users unless reactivated.'
            })

        next_domain = attrs.get('domain', self.instance.domain if self.instance else None)
        if self.instance is not None and next_domain.id != self.instance.domain_id:
            raise serializers.ValidationError({
                'domain': 'A Contract cannot be moved to another Domain. Copy it instead.',
            })
        next_facilities = attrs.get('facilities')
        if next_domain and next_facilities is not None:
            invalid_facilities = [
                facility.name for facility in next_facilities
                if facility.region_id != next_domain.region_id
            ]
            if invalid_facilities:
                raise serializers.ValidationError({
                    'facility_ids': (
                        'Every selected Facility must belong to the Contract Region. '
                        f'Invalid: {", ".join(sorted(invalid_facilities))}.'
                    ),
                })

        if assigned_user_ids:
            has_facilities = (
                bool(next_facilities)
                if next_facilities is not None
                else bool(self.instance and self.instance.facilities.exists())
            )
            if not has_facilities:
                raise serializers.ValidationError({
                    'assigned_user_ids': (
                        'Select at least one Facility before assigning users to this Contract.'
                    ),
                })

        if self.instance is not None:
            shared_rule_settings = attrs.get('shared_rule_settings')
            if shared_rule_settings is not None:
                links = {
                    link.shared_rule_id: link
                    for link in self.instance.shared_rule_links.all()
                }
                normalized = []
                seen = set()
                for index, row in enumerate(shared_rule_settings, start=1):
                    try:
                        shared_rule_id = int(row.get('shared_rule_id') or 0)
                    except (TypeError, ValueError):
                        shared_rule_id = 0
                    if shared_rule_id not in links or shared_rule_id in seen:
                        raise serializers.ValidationError({
                            'shared_rule_settings': (
                                'Only Shared Rules already linked to this contract '
                                'can be adjusted here.'
                            ),
                        })
                    seen.add(shared_rule_id)
                    values = {'shared_rule_id': shared_rule_id}
                    for field in (
                        'min_value', 'max_value',
                        'min_penalty_weight', 'max_penalty_weight',
                    ):
                        value = row.get(field)
                        if value in (None, ''):
                            values[field] = None
                            continue
                        try:
                            decimal_value = Decimal(str(value))
                        except (InvalidOperation, TypeError, ValueError):
                            raise serializers.ValidationError({
                                'shared_rule_settings': (
                                    f'Shared Rule row {index} has an invalid '
                                    f'{field.replace("_", " ")}.'
                                ),
                            })
                        if decimal_value != decimal_value.to_integral_value():
                            raise serializers.ValidationError({
                                'shared_rule_settings': (
                                    f'Shared Rule row {index} requires whole '
                                    f'numbers for {field.replace("_", " ")}.'
                                ),
                            })
                        values[field] = int(decimal_value)
                    if (
                        values['min_value'] is not None
                        and values['max_value'] is not None
                        and values['min_value'] > values['max_value']
                    ):
                        raise serializers.ValidationError({
                            'shared_rule_settings': (
                                f'Shared Rule row {index} has a minimum '
                                'greater than its maximum.'
                            ),
                        })
                    if any(
                        values[field] is not None and values[field] < 0
                        for field in ('min_penalty_weight', 'max_penalty_weight')
                    ):
                        raise serializers.ValidationError({
                            'shared_rule_settings': (
                                f'Shared Rule row {index} has a negative penalty.'
                            ),
                        })
                    normalized.append(values)
                attrs['shared_rule_settings'] = normalized

            next_domain = attrs.get('domain', self.instance.domain)
            next_facilities = attrs.get('facilities')
            facility_ids = (
                {facility.id for facility in next_facilities}
                if next_facilities is not None
                else set(self.instance.facilities.values_list('id', flat=True))
            )
            conflicts = []
            for link in self.instance.shared_rule_links.select_related(
                'shared_rule',
            ).prefetch_related('shared_rule__shift_templates'):
                if not link.enabled or not link.shared_rule.active:
                    continue
                rule = link.shared_rule
                rule_facilities = {
                    template.facility_id
                    for template in rule.shift_templates.all()
                }
                if rule.domain_id != next_domain.id or not (
                    rule_facilities & facility_ids
                ):
                    conflicts.append(rule.name)
            if conflicts:
                raise serializers.ValidationError({
                    'facility_ids': (
                        'This change contradicts these active Shared Rules: '
                        f'{", ".join(sorted(conflicts))}.'
                    ),
                })

        return attrs

    def _save_assignments(self, contract, assigned_user_ids):
        if assigned_user_ids is None:
            return

        if DomainMembership.objects.filter(domain=contract.domain).exists():
            working_user_ids = DomainMembership.objects.filter(
                domain=contract.domain,
                active=True,
                clinically_active=True,
            ).values_list('user_id', flat=True)
            eligible_physician_ids = set(
                Physician.objects.filter(
                    id__in=assigned_user_ids,
                    user_id__in=working_user_ids,
                ).values_list('id', flat=True)
            )
            if set(assigned_user_ids) - eligible_physician_ids:
                raise serializers.ValidationError({
                    'assigned_user_ids': (
                        'Every assigned user must have working access to this Contract domain.'
                    ),
                })

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
        validated_data.pop('shared_rule_settings', None)
        facilities = validated_data.pop('facilities', [])

        contract = Contract.objects.create(**validated_data)
        contract.facilities.set(facilities)
        self._save_assignments(contract, assigned_user_ids)
        return contract

    def update(self, instance, validated_data):
        assigned_user_ids = validated_data.pop('assigned_user_ids', None)
        shared_rule_settings = validated_data.pop('shared_rule_settings', None)
        facilities = validated_data.pop('facilities', None)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()

        if facilities is not None:
            instance.facilities.set(facilities)

        self._save_assignments(instance, assigned_user_ids)
        if shared_rule_settings is not None:
            links = {
                link.shared_rule_id: link
                for link in instance.shared_rule_links.all()
            }
            for row in shared_rule_settings:
                link = links[row['shared_rule_id']]
                for field in (
                    'min_value', 'max_value',
                    'min_penalty_weight', 'max_penalty_weight',
                ):
                    setattr(link, field, row[field])
                link.save(update_fields=[
                    'min_value', 'max_value',
                    'min_penalty_weight', 'max_penalty_weight', 'updated_at',
                ])
        for shared_rule in instance.shared_rules.all():
            sync_shared_rule_contract_settings(shared_rule)
        return instance


class SharedRuleSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    region = serializers.IntegerField(source='domain.region_id', read_only=True)
    region_name = serializers.CharField(source='domain.region.name', read_only=True)
    shift_template_ids = serializers.PrimaryKeyRelatedField(
        source='shift_templates', many=True,
        queryset=ShiftTemplate.objects.filter(active=True),
    )
    shift_templates = serializers.SerializerMethodField()
    contract_settings = serializers.ListField(
        child=serializers.DictField(), write_only=True,
    )
    contracts = serializers.SerializerMethodField()
    setup_required = serializers.SerializerMethodField()

    class Meta:
        model = SharedRule
        fields = [
            'id', 'domain', 'domain_name', 'region', 'region_name', 'name', 'active',
            'period_type', 'units', 'shift_template_ids', 'shift_templates',
            'contract_settings', 'contracts', 'reference_contract_settings',
            'reference_shift_templates',
            'setup_required', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'domain_name', 'region', 'region_name', 'shift_templates', 'contracts',
            'reference_contract_settings', 'reference_shift_templates',
            'setup_required', 'created_at', 'updated_at',
        ]

    def get_shift_templates(self, obj):
        return [{
            'id': template.id,
            'name': template.generated_name(),
            'facility_id': template.facility_id,
            'facility_name': template.facility.name,
        } for template in obj.shift_templates.all()]

    def get_contracts(self, obj):
        return [{
            'id': link.contract_id,
            'name': link.contract.name,
            'active': link.contract.active,
            'enabled': link.enabled,
            'min_value': (
                str(int(link.min_value)) if link.min_value is not None else ''
            ),
            'max_value': (
                str(int(link.max_value)) if link.max_value is not None else ''
            ),
            'min_penalty_weight': (
                str(int(link.min_penalty_weight))
                if link.min_penalty_weight is not None else ''
            ),
            'max_penalty_weight': (
                str(int(link.max_penalty_weight))
                if link.max_penalty_weight is not None else ''
            ),
            'spread_violations': link.spread_violations,
        } for link in obj.contract_links.all()]

    def get_setup_required(self, obj):
        return (
            obj.shift_templates.count() == 0
            or obj.contract_links.count() < 1
        )

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Name is required.')
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        domain = attrs.get('domain', getattr(self.instance, 'domain', None))
        if self.instance is not None and domain.id != self.instance.domain_id:
            raise serializers.ValidationError({
                'domain': 'A Shared Rule cannot be moved to another Domain. Copy it instead.',
            })
        templates = attrs.get(
            'shift_templates',
            list(self.instance.shift_templates.all()) if self.instance else [],
        )
        contract_settings = attrs.get('contract_settings')
        if contract_settings is None and self.instance is not None:
            contract_settings = [
                {'contract_id': link.contract_id}
                for link in self.instance.contract_links.all()
            ]
        contract_settings = contract_settings or []
        normalized_settings = []
        for index, row in enumerate(contract_settings, start=1):
            normalized = dict(row)
            for field in (
                'min_value', 'max_value',
                'min_penalty_weight', 'max_penalty_weight',
            ):
                value = row.get(field)
                if value in (None, ''):
                    normalized[field] = None
                    continue
                try:
                    decimal_value = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError):
                    raise serializers.ValidationError({
                        'contract_settings': (
                            f'Contract row {index} has an invalid {field.replace("_", " ")}.'
                        ),
                    })
                if decimal_value != decimal_value.to_integral_value():
                    raise serializers.ValidationError({
                        'contract_settings': (
                            f'Contract row {index} requires whole numbers for '
                            f'{field.replace("_", " ")}.'
                        ),
                    })
                normalized[field] = int(decimal_value)
            if (
                normalized['min_value'] is not None
                and normalized['max_value'] is not None
                and normalized['min_value'] > normalized['max_value']
            ):
                raise serializers.ValidationError({
                    'contract_settings': (
                        f'Contract row {index} has a minimum greater than its maximum.'
                    ),
                })
            for field in ('min_penalty_weight', 'max_penalty_weight'):
                if normalized[field] is not None and normalized[field] < 0:
                    raise serializers.ValidationError({
                        'contract_settings': (
                            f'Contract row {index} has a negative penalty.'
                        ),
                    })
            normalized_settings.append(normalized)
        contract_settings = normalized_settings
        attrs['contract_settings'] = normalized_settings
        contract_ids = [
            int(row.get('contract_id') or 0) for row in contract_settings
        ]
        if len(set(contract_ids)) < 1:
            raise serializers.ValidationError({
                'contract_settings': (
                    'Select at least one contract for a Shared Rule.'
                ),
            })
        if len(set(contract_ids)) != len(contract_ids) or 0 in contract_ids:
            raise serializers.ValidationError({
                'contract_settings': 'Each selected contract must be unique.',
            })
        contracts = list(
            Contract.objects.filter(id__in=contract_ids)
            .prefetch_related('facilities')
        )
        if len(contracts) != len(contract_ids):
            raise serializers.ValidationError({
                'contract_settings': 'One or more contracts do not exist.',
            })
        if any(contract.domain_id != domain.id for contract in contracts):
            raise serializers.ValidationError({
                'contract_settings': (
                    'All Shared Rule contracts must use the selected domain.'
                ),
            })
        if any(template.domain_id != domain.id for template in templates):
            raise serializers.ValidationError({
                'shift_template_ids': (
                    'Every selected shift must use the Shared Rule domain.'
                ),
            })
        if not templates:
            raise serializers.ValidationError({
                'shift_template_ids': 'Select at least one shift.',
            })
        template_facilities = {template.facility_id for template in templates}
        incompatible = [
            contract.name for contract in contracts
            if not template_facilities.intersection(
                contract.facilities.values_list('id', flat=True)
            )
        ]
        if incompatible:
            raise serializers.ValidationError({
                'contract_settings': (
                    'These contracts exclude every facility represented by '
                    f'this rule: {", ".join(sorted(incompatible))}.'
                ),
            })
        attrs['_validated_contracts'] = {c.id: c for c in contracts}
        return attrs

    @staticmethod
    def _number(value):
        if value in (None, ''):
            return None
        return value

    def _save_links(self, shared_rule, rows):
        SharedRuleContract.objects.filter(shared_rule=shared_rule).delete()
        SharedRuleContract.objects.bulk_create([
            SharedRuleContract(
                shared_rule=shared_rule,
                contract_id=int(row['contract_id']),
                enabled=bool(row.get('enabled', True)),
                min_value=self._number(row.get('min_value')),
                max_value=self._number(row.get('max_value')),
                min_penalty_weight=self._number(
                    row.get('min_penalty_weight')
                ),
                max_penalty_weight=self._number(
                    row.get('max_penalty_weight')
                ),
                spread_violations=bool(row.get('spread_violations', True)),
            )
            for row in rows
        ])

    def create(self, validated_data):
        rows = validated_data.pop('contract_settings')
        validated_data.pop('_validated_contracts', None)
        templates = validated_data.pop('shift_templates')
        shared_rule = SharedRule.objects.create(**validated_data)
        shared_rule.shift_templates.set(templates)
        self._save_links(shared_rule, rows)
        sync_shared_rule_contract_settings(shared_rule)
        return shared_rule

    def update(self, instance, validated_data):
        rows = validated_data.pop('contract_settings', None)
        validated_data.pop('_validated_contracts', None)
        templates = validated_data.pop('shift_templates', None)
        previous_contract_ids = list(
            instance.contract_links.values_list('contract_id', flat=True)
        )
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if templates is not None:
            instance.shift_templates.set(templates)
        if rows is not None:
            self._save_links(instance, rows)
        instance.reference_contract_settings = []
        instance.reference_shift_templates = []
        instance.save(update_fields=[
            'reference_contract_settings', 'reference_shift_templates',
            'updated_at',
        ])
        sync_shared_rule_contract_settings(instance, previous_contract_ids)
        return instance
