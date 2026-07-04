from datetime import datetime, timedelta

from rest_framework import serializers
from .models import Shift, ShiftTemplate


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
