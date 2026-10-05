from rest_framework import serializers

from .models import Domain, DomainMembership, Organization, OrganizationMembership, Region


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
        fields = ['id', 'organization', 'user', 'user_name', 'user_email']
        read_only_fields = ['id', 'organization', 'user_name', 'user_email']

    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.email or obj.user.username


class DomainMembershipSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source='user.email', read_only=True)
    domain_name = serializers.CharField(source='domain.name', read_only=True)

    class Meta:
        model = DomainMembership
        fields = [
            'id', 'domain', 'domain_name', 'user', 'user_name', 'user_email', 'role',
        ]
        read_only_fields = ['id', 'domain', 'domain_name', 'user_name', 'user_email']

    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.email or obj.user.username

    def validate(self, attrs):
        domain = attrs.get('domain') or getattr(self.instance, 'domain', None)
        user = attrs.get('user') or getattr(self.instance, 'user', None)
        if domain and user and not OrganizationMembership.objects.filter(
            organization=domain.region.organization,
            user=user,
        ).exists():
            raise serializers.ValidationError({
                'user': 'User must belong to the organization before joining a domain.',
            })
        return attrs
