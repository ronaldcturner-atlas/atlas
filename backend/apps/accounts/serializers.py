from rest_framework import serializers
from django.contrib.auth.models import User

from apps.facilities.models import Facility

from .models import Physician


class UserSerializer(serializers.ModelSerializer):
    physician_id = serializers.SerializerMethodField()
    groups = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'is_staff', 'is_superuser', 'physician_id', 'groups']

    def get_physician_id(self, obj):
        physician = getattr(obj, 'physician', None)
        return physician.id if physician else None

    def get_groups(self, obj):
        return list(obj.groups.values_list('name', flat=True))


class PhysicianSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(source='user.first_name')
    last_name = serializers.CharField(source='user.last_name')
    email = serializers.EmailField(source='user.email')
    primary_facility_name = serializers.CharField(source='primary_facility.name', read_only=True)

    class Meta:
        model = Physician
        fields = [
            'id',
            'first_name',
            'last_name',
            'display_name',
            'email',
            'primary_facility',
            'primary_facility_name',
            'clinician_type',
            'fte',
            'active',
        ]
        read_only_fields = ['id', 'primary_facility_name']

    def validate_email(self, value):
        normalized = value.strip().lower()
        existing_user = User.objects.filter(email__iexact=normalized).first()

        if not existing_user:
            return normalized

        if self.instance and self.instance.user_id == existing_user.id:
            return normalized

        raise serializers.ValidationError('A user with this email already exists.')

    def validate_primary_facility(self, value):
        if value is None:
            return value

        if not Facility.objects.filter(id=value.id).exists():
            raise serializers.ValidationError('Selected facility does not exist.')
        return value

    def create(self, validated_data):
        user_data = validated_data.pop('user')
        email = user_data.get('email', '').strip().lower()
        first_name = user_data.get('first_name', '').strip()
        last_name = user_data.get('last_name', '').strip()

        user = User.objects.create(
            username=email,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

        return Physician.objects.create(user=user, **validated_data)

    def update(self, instance, validated_data):
        user_data = validated_data.pop('user', {})
        user = instance.user

        if 'first_name' in user_data:
            user.first_name = user_data['first_name'].strip()

        if 'last_name' in user_data:
            user.last_name = user_data['last_name'].strip()

        if 'email' in user_data:
            email = user_data['email'].strip().lower()
            user.email = email
            user.username = email

        user.save(update_fields=['first_name', 'last_name', 'email', 'username'])

        for field, value in validated_data.items():
            setattr(instance, field, value)

        instance.save()
        return instance

