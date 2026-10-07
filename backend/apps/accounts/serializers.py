from rest_framework import serializers
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Q

from apps.facilities.models import Facility
from apps.domains.permissions import CLINICAL_PERMISSIONS, membership_permissions, is_org_admin

from .models import Physician


class UserSerializer(serializers.ModelSerializer):
    physician_id = serializers.SerializerMethodField()
    groups = serializers.SerializerMethodField()
    organization_memberships = serializers.SerializerMethodField()
    is_org_admin = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    domain_access = serializers.SerializerMethodField()
    can_manage_schedules = serializers.SerializerMethodField()
    can_test_access = serializers.SerializerMethodField()
    test_access = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'is_staff',
            'is_superuser', 'physician_id', 'groups', 'organization_memberships',
            'is_org_admin',
            'permissions', 'domain_access', 'can_manage_schedules',
            'can_test_access', 'test_access',
        ]

    def get_physician_id(self, obj):
        physician = getattr(obj, 'physician', None)
        return physician.id if physician else None

    def get_groups(self, obj):
        if getattr(obj, '_atlas_test_access_active', False):
            return []
        return list(obj.groups.values_list('name', flat=True))

    def get_organization_memberships(self, obj):
        return [
            {
                'organization_id': membership.organization_id,
                'organization_name': membership.organization.name,
                'is_org_admin': membership.is_org_admin,
                'active': membership.active,
            }
            for membership in obj.organization_memberships.select_related('organization').all()
        ]

    def get_is_org_admin(self, obj):
        return is_org_admin(obj)

    def get_permissions(self, obj):
        if getattr(obj, '_atlas_test_access_active', False):
            permissions = set(obj._atlas_test_role_template.permissions or [])
            if not obj._atlas_test_clinically_active:
                permissions.difference_update(CLINICAL_PERMISSIONS)
            return sorted(permissions)
        if is_org_admin(obj):
            from apps.domains.permissions import ALL_PERMISSIONS
            return sorted(ALL_PERMISSIONS)
        permissions = set()
        for membership in obj.domain_memberships.filter(active=True).select_related('role_template'):
            permissions.update(membership_permissions(membership))
        return sorted(permissions)

    def get_domain_access(self, obj):
        if getattr(obj, '_atlas_test_access_active', False):
            domain = obj._atlas_test_domain
            role = obj._atlas_test_role_template
            permissions = set(role.permissions or [])
            if not obj._atlas_test_clinically_active:
                permissions.difference_update(CLINICAL_PERMISSIONS)
            return [{
                'domain_id': domain.id,
                'domain_name': domain.name,
                'region_id': domain.region_id,
                'region_name': domain.region.name,
                'role_template_id': role.id,
                'role_name': role.name,
                'clinically_active': obj._atlas_test_clinically_active,
                'active': True,
                'permissions': sorted(permissions),
            }]
        return [
            {
                'domain_id': membership.domain_id,
                'domain_name': membership.domain.name,
                'region_id': membership.domain.region_id,
                'region_name': membership.domain.region.name,
                'role_template_id': membership.role_template_id,
                'role_name': membership.role_template.name if membership.role_template else membership.get_role_display(),
                'clinically_active': membership.clinically_active,
                'active': membership.active,
                'permissions': sorted(membership_permissions(membership)),
            }
            for membership in obj.domain_memberships.select_related(
                'domain__region', 'role_template',
            ).all()
        ]

    def get_can_manage_schedules(self, obj):
        management_permissions = {
            'manage_build_workspace', 'manage_published_assignments',
            'administer_requests',
            'manage_shift_templates', 'manage_regional_facilities',
            'manage_domains', 'view_roles', 'create_users',
            'edit_user_profiles', 'manage_domain_access',
        }
        if getattr(obj, '_atlas_test_access_active', False):
            return bool(management_permissions.intersection(self.get_permissions(obj)))
        return is_org_admin(obj) or any(
            management_permissions.intersection(membership_permissions(membership))
            for membership in obj.domain_memberships.filter(active=True).select_related('role_template')
        )

    def get_can_test_access(self, obj):
        return bool(
            settings.DEBUG
            and (
                getattr(obj, '_atlas_test_actual_org_admin', False)
                or obj.is_superuser
                or obj.organization_memberships.filter(active=True, is_org_admin=True).exists()
            )
        )

    def get_test_access(self, obj):
        if not getattr(obj, '_atlas_test_access_active', False):
            return None
        return {
            'domain_id': obj._atlas_test_domain.id,
            'domain_name': obj._atlas_test_domain.name,
            'region_id': obj._atlas_test_domain.region_id,
            'region_name': obj._atlas_test_domain.region.name,
            'role_template_id': obj._atlas_test_role_template.id,
            'role_name': obj._atlas_test_role_template.name,
            'clinically_active': obj._atlas_test_clinically_active,
        }


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
                'role_template_id': membership.role_template_id,
                'role_name': membership.role_template.name if membership.role_template else membership.get_role_display(),
                'clinically_active': membership.clinically_active,
                'active': membership.active,
            }
            for membership in obj.user.domain_memberships.all()
        ]

    def get_organization_memberships(self, obj):
        return [
            {
                'id': membership.id,
                'organization_id': membership.organization_id,
                'organization_name': membership.organization.name,
                'is_org_admin': membership.is_org_admin,
                'active': membership.active,
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

