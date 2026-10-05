from rest_framework import serializers
from django.contrib.auth.models import User
from django.db.models import Q

from apps.facilities.models import Facility

from .models import Physician


class UserSerializer(serializers.ModelSerializer):
    physician_id = serializers.SerializerMethodField()
    groups = serializers.SerializerMethodField()
    organization_memberships = serializers.SerializerMethodField()
    is_org_admin = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'is_staff',
            'is_superuser', 'physician_id', 'groups', 'organization_memberships',
            'is_org_admin',
        ]

    def get_physician_id(self, obj):
        physician = getattr(obj, 'physician', None)
        return physician.id if physician else None

    def get_groups(self, obj):
        return list(obj.groups.values_list('name', flat=True))

    def get_organization_memberships(self, obj):
        return [
            {
                'organization_id': membership.organization_id,
                'organization_name': membership.organization.name,
            }
            for membership in obj.organization_memberships.select_related('organization').all()
        ]

    def get_is_org_admin(self, obj):
        return obj.is_superuser or obj.domain_memberships.filter(role='org_admin').exists()


class PhysicianSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source='user.id', read_only=True)
    first_name = serializers.CharField(source='user.first_name')
    last_name = serializers.CharField(source='user.last_name')
    email = serializers.EmailField(source='user.email', required=True, allow_blank=False)
    primary_facility_name = serializers.CharField(source='primary_facility.name', read_only=True)
    current_contracts = serializers.SerializerMethodField()
    domain_memberships = serializers.SerializerMethodField()
    organization_memberships = serializers.SerializerMethodField()

    class Meta:
        model = Physician
        fields = [
            'id',
            'user_id',
            'first_name',
            'last_name',
            'display_name',
            'email',
            'phone_number',
            'current_contracts',
            'domain_memberships',
            'organization_memberships',
            'role',
            'primary_facility',
            'primary_facility_name',
            'clinician_type',
            'fte',
            'active',
        ]
        read_only_fields = [
            'id', 'current_contracts', 'domain_memberships',
            'organization_memberships', 'primary_facility_name',
        ]

    def get_current_contracts(self, obj):
        assignments = obj.contract_assignments.all()
        return [
            {
                'id': assignment.contract_id,
                'name': assignment.contract.name,
                'domain_id': assignment.domain_id,
                'domain': assignment.contract.domain.name,
            }
            for assignment in assignments
        ]

    def get_domain_memberships(self, obj):
        return [
            {
                'id': membership.id,
                'domain_id': membership.domain_id,
                'domain_name': membership.domain.name,
                'organization_id': membership.domain.region.organization_id,
                'region_id': membership.domain.region_id,
                'region_name': membership.domain.region.name,
                'role': membership.role,
            }
            for membership in obj.user.domain_memberships.all()
        ]

    def get_organization_memberships(self, obj):
        return [
            {
                'id': membership.id,
                'organization_id': membership.organization_id,
                'organization_name': membership.organization.name,
            }
            for membership in obj.user.organization_memberships.all()
        ]

    def validate_email(self, value):
        normalized = value.strip().lower()
        existing_users = User.objects.filter(
            Q(email__iexact=normalized) | Q(username__iexact=normalized),
        )
        if self.instance:
            existing_users = existing_users.exclude(id=self.instance.user_id)
        if existing_users.exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return normalized

    def validate_phone_number(self, value):
        return value.strip()

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

