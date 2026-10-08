from rest_framework import serializers

from .models import (
    AuditEvent,
    Domain,
    DomainMembership,
    Organization,
    OrganizationMembership,
    Region,
    RoleTemplate,
)
from .permissions import ALL_PERMISSIONS, PERMISSION_GROUPS


class OrganizationSerializer(serializers.ModelSerializer):
    domain_count = serializers.IntegerField(read_only=True)
    region_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Organization
        fields = ['id', 'name', 'active', 'region_count', 'domain_count', 'created_at', 'updated_at']
        read_only_fields = ['id', 'region_count', 'domain_count', 'created_at', 'updated_at']


class RegionSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    domain_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Region
        fields = [
            'id', 'organization', 'organization_name', 'name', 'active',
            'domain_count', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'organization', 'organization_name', 'domain_count', 'created_at', 'updated_at',
        ]

    def validate_name(self, value):
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError('Region name is required.')
        return normalized


class DomainSerializer(serializers.ModelSerializer):
    region_name = serializers.CharField(source='region.name', read_only=True)
    organization = serializers.IntegerField(source='region.organization_id', read_only=True)
    organization_name = serializers.CharField(source='region.organization.name', read_only=True)
    membership_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Domain
        fields = [
            'id', 'region', 'region_name', 'organization', 'organization_name', 'name', 'active',
            'membership_count', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'region_name', 'organization', 'organization_name', 'membership_count', 'created_at', 'updated_at',
        ]

    def validate_name(self, value):
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError('Domain name is required.')
        return normalized


class OrganizationMembershipSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = OrganizationMembership
        fields = ['id', 'organization', 'user', 'user_name', 'user_email', 'is_org_admin', 'active']
        read_only_fields = ['id', 'organization', 'user_name', 'user_email']

    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.email or obj.user.username


class DomainMembershipSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source='user.email', read_only=True)
    domain_name = serializers.CharField(source='domain.name', read_only=True)
    role_template_name = serializers.CharField(source='role_template.name', read_only=True)
    effective_permissions = serializers.SerializerMethodField()

    class Meta:
        model = DomainMembership
        fields = [
            'id', 'domain', 'domain_name', 'user', 'user_name', 'user_email', 'role',
            'role_template', 'role_template_name', 'clinically_active', 'active',
            'effective_permissions',
        ]
        read_only_fields = [
            'id', 'domain', 'domain_name', 'user_name', 'user_email',
            'role_template_name', 'effective_permissions',
        ]
        extra_kwargs = {'role': {'required': False}}

    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.email or obj.user.username

    def get_effective_permissions(self, obj):
        from .permissions import membership_permissions
        return sorted(membership_permissions(obj))

    def validate(self, attrs):
        domain = attrs.get('domain') or getattr(self.instance, 'domain', None)
        user = attrs.get('user') or getattr(self.instance, 'user', None)
        if domain and user and not OrganizationMembership.objects.filter(
            organization=domain.region.organization,
            user=user,
            active=True,
        ).exists():
            raise serializers.ValidationError({
                'user': 'User must belong to the organization before joining a domain.',
            })
        role_template = attrs.get('role_template') or getattr(self.instance, 'role_template', None)
        if domain and role_template and role_template.region_id != domain.region_id:
            raise serializers.ValidationError({
                'role_template': 'The selected Role must belong to this Domain’s Region.',
            })
        if role_template:
            attrs['role'] = role_template.system_key or role_template.name.lower().replace(' ', '_')
        clinically_active = attrs.get(
            'clinically_active',
            getattr(self.instance, 'clinically_active', True),
        )
        if role_template and role_template.system_key == 'view_only' and clinically_active:
            raise serializers.ValidationError({
                'clinically_active': 'View Only access cannot be clinically active.',
            })
        return attrs


class RoleTemplateSerializer(serializers.ModelSerializer):
    region_name = serializers.CharField(source='region.name', read_only=True)
    assigned_user_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = RoleTemplate
        fields = [
            'id', 'region', 'region_name', 'name', 'system_key', 'permissions',
            'notification_defaults', 'active', 'assigned_user_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'region', 'region_name', 'system_key', 'assigned_user_count',
            'created_at', 'updated_at',
        ]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Role name is required.')
        if value.casefold() == 'org admin':
            raise serializers.ValidationError('Org Admin is protected and cannot be created as a regional Role.')
        return value

    def validate_permissions(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Permissions must be a list.')
        invalid = sorted(set(value) - ALL_PERMISSIONS)
        if invalid:
            raise serializers.ValidationError(f"Unknown permissions: {', '.join(invalid)}")
        return sorted(set(value))


class AuditEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    target_user_name = serializers.SerializerMethodField()
    region_name = serializers.CharField(source='region.name', read_only=True)
    domain_name = serializers.CharField(source='domain.name', read_only=True)

    class Meta:
        model = AuditEvent
        fields = [
            'id', 'organization', 'region', 'region_name', 'domain', 'domain_name',
            'actor', 'actor_name', 'target_user', 'target_user_name', 'action',
            'details', 'created_at',
        ]

    def get_actor_name(self, obj):
        return obj.actor.get_full_name() or obj.actor.email if obj.actor else 'Atlas'

    def get_target_user_name(self, obj):
        return obj.target_user.get_full_name() or obj.target_user.email if obj.target_user else None


def permission_catalog_payload():
    return [
        {
            'name': group_name,
            'permissions': [
                {'code': code, 'label': label}
                for code, label in permissions.items()
            ],
        }
        for group_name, permissions in PERMISSION_GROUPS.items()
    ]
