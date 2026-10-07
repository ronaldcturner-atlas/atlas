from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import (
	AuditEvent,
	Domain,
	DomainMembership,
	Organization,
	OrganizationMembership,
	Region,
	RoleTemplate,
)
from .permissions import ALL_PERMISSIONS, DEFAULT_ROLE_DEFINITIONS, has_permission, is_org_admin, permitted_domain_ids
from .serializers import (
	AuditEventSerializer,
	DomainMembershipSerializer,
	DomainSerializer,
	OrganizationMembershipSerializer,
	OrganizationSerializer,
	RegionSerializer,
	RoleTemplateSerializer,
	permission_catalog_payload,
)


class CsrfExemptSessionAuthentication(SessionAuthentication):
	def enforce_csrf(self, request):
		return


def _ensure_default_organization():
	organization, _ = Organization.objects.get_or_create(
		name='Lowcountry Emergency Physicians',
		defaults={'active': True},
	)
	region, _ = Region.objects.get_or_create(
		organization=organization,
		name=organization.name,
		defaults={'active': True},
	)
	Domain.objects.get_or_create(
		region=region,
		name='Physician',
		defaults={'active': True},
	)
	return organization


def _organizations_for_user(user):
	if user.is_superuser:
		return Organization.objects.all()
	return Organization.objects.filter(memberships__user=user).distinct()


def _can_access_organization(user, organization):
	return user.is_superuser or OrganizationMembership.objects.filter(
		organization=organization,
		user=user,
	).exists()


def _is_org_admin(user, organization):
	return is_org_admin(user, organization)


def _record_audit(*, request, organization, action, region=None, domain=None, target_user=None, details=None):
	AuditEvent.objects.create(
		organization=organization,
		region=region,
		domain=domain,
		actor=request.user,
		target_user=target_user,
		action=action,
		details=details or {},
	)


def _ensure_region_roles(region):
	roles = {}
	for system_key, name, permissions in DEFAULT_ROLE_DEFINITIONS:
		role, _ = RoleTemplate.objects.get_or_create(
			region=region,
			name=name,
			defaults={
				'system_key': system_key,
				'permissions': sorted(permissions),
				'notification_defaults': {},
				'active': True,
			},
		)
		roles[system_key] = role
	return roles


def _has_domain_contract_assignment(membership):
	physician = getattr(membership.user, 'physician', None)
	if physician is None:
		return False
	from apps.scheduling.models import ContractUserAssignment
	return ContractUserAssignment.objects.filter(
		domain=membership.domain,
		physician=physician,
	).exists()


def _organization_from_request(request):
	organization_id = request.data.get('organization') if request.method == 'POST' else request.query_params.get('organization')
	if organization_id:
		return get_object_or_404(_organizations_for_user(request.user), id=organization_id)
	organization = _organizations_for_user(request.user).order_by('name', 'id').first()
	if organization:
		return organization
	organization = _ensure_default_organization()
	if not organization.memberships.exists():
		OrganizationMembership.objects.create(organization=organization, user=request.user)
		default_domain = Domain.objects.filter(region__organization=organization).order_by('id').first()
		roles = _ensure_region_roles(default_domain.region)
		OrganizationMembership.objects.filter(organization=organization, user=request.user).update(is_org_admin=True)
		DomainMembership.objects.create(
			domain=default_domain,
			user=request.user,
			role=DomainMembership.Role.REGIONAL_ADMIN,
			role_template=roles['regional_admin'],
		)
		return organization
	return None


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def organizations_list_create(request):
	if request.method == 'GET':
		_ensure_default_organization()
		organizations = _organizations_for_user(request.user).annotate(
			region_count=Count('regions', distinct=True),
			domain_count=Count('regions__domains', distinct=True),
		)
		return Response(OrganizationSerializer(organizations, many=True).data)

	if Organization.objects.exists() and not request.user.is_superuser:
		return Response(
			{'detail': 'Only a system administrator can create another organization.'},
			status=status.HTTP_403_FORBIDDEN,
		)
	serializer = OrganizationSerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	organization = serializer.save()
	OrganizationMembership.objects.create(organization=organization, user=request.user, is_org_admin=True)
	default_region = Region.objects.create(organization=organization, name=organization.name)
	roles = _ensure_region_roles(default_region)
	default_domain = Domain.objects.create(region=default_region, name='Physician')
	DomainMembership.objects.create(
		domain=default_domain,
		user=request.user,
		role=DomainMembership.Role.REGIONAL_ADMIN,
		role_template=roles['regional_admin'],
	)
	return Response(OrganizationSerializer(organization).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def organization_detail(request, organization_id):
	organization = get_object_or_404(Organization, id=organization_id)
	if not _can_access_organization(request.user, organization):
		return Response({'detail': 'Organization access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'GET':
		organization.region_count = organization.regions.count()
		organization.domain_count = Domain.objects.filter(region__organization=organization).count()
		return Response(OrganizationSerializer(organization).data)
	if not _is_org_admin(request.user, organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	serializer = OrganizationSerializer(organization, data=request.data, partial=True)
	serializer.is_valid(raise_exception=True)
	serializer.save()
	return Response(serializer.data)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def regions_list_create(request, organization_id):
	organization = get_object_or_404(Organization, id=organization_id)
	if not _can_access_organization(request.user, organization):
		return Response({'detail': 'Organization access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'GET':
		regions = organization.regions.annotate(domain_count=Count('domains'))
		if getattr(request.user, '_atlas_test_access_active', False):
			regions = regions.filter(id=request.user._atlas_test_domain.region_id)
		elif not _is_org_admin(request.user, organization):
			regions = regions.filter(
				domains__memberships__user=request.user,
				domains__memberships__active=True,
			).distinct()
		payload = RegionSerializer(regions, many=True).data
		for row in payload:
			region = next(region for region in regions if region.id == row['id'])
			row['can_manage_facilities'] = has_permission(
				request.user, 'manage_regional_facilities', region=region,
			)
		return Response(payload)
	if not _is_org_admin(request.user, organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	serializer = RegionSerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	region = serializer.save(organization=organization)
	_ensure_region_roles(region)
	_record_audit(request=request, organization=organization, region=region, action='region.created', details={'name': region.name})
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def region_detail(request, region_id):
	region = get_object_or_404(Region.objects.select_related('organization'), id=region_id)
	organization = region.organization
	if not _can_access_organization(request.user, organization):
		return Response({'detail': 'Organization access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'GET' and not (
		_is_org_admin(request.user, organization)
		or has_permission(request.user, 'view_published_schedules', region=region)
		or has_permission(request.user, 'manage_regional_facilities', region=region)
	):
		return Response({'detail': 'Region access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'GET':
		region.domain_count = region.domains.count()
		return Response(RegionSerializer(region).data)
	if not _is_org_admin(request.user, organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'DELETE':
		if region.domains.exists():
			return Response({'detail': 'Move or delete this region’s domains before deleting it.'}, status=status.HTTP_409_CONFLICT)
		if region.active and organization.regions.filter(active=True).count() <= 1:
			return Response({'detail': 'An organization must retain at least one active region.'}, status=status.HTTP_409_CONFLICT)
		region.delete()
		return Response(status=status.HTTP_204_NO_CONTENT)
	if request.data.get('active') is False and region.active and organization.regions.filter(active=True).count() <= 1:
		return Response({'detail': 'An organization must retain at least one active region.'}, status=status.HTTP_409_CONFLICT)
	serializer = RegionSerializer(region, data=request.data, partial=True)
	serializer.is_valid(raise_exception=True)
	serializer.save()
	return Response(serializer.data)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def domains_list_create(request):
	if request.method == 'GET':
		_ensure_default_organization()
		organization = _organization_from_request(request)
		if organization is None:
			return Response([])
		domains = Domain.objects.filter(region__organization=organization).select_related('region__organization').annotate(
			membership_count=Count('memberships'),
		)
		permission_filter = request.query_params.get('permission')
		permission_filters = [
			value.strip()
			for value in request.query_params.get('permissions', '').split(',')
			if value.strip()
		]
		if permission_filter and permission_filters:
			return Response(
				{'permissions': ['Use either permission or permissions, not both.']},
				status=status.HTTP_400_BAD_REQUEST,
			)
		if permission_filter:
			if permission_filter not in ALL_PERMISSIONS:
				return Response({'permission': ['Unknown permission.']}, status=status.HTTP_400_BAD_REQUEST)
			domains = domains.filter(id__in=permitted_domain_ids(request.user, permission_filter))
		elif permission_filters:
			unknown_permissions = sorted(set(permission_filters) - ALL_PERMISSIONS)
			if unknown_permissions:
				return Response(
					{'permissions': [f'Unknown permission: {unknown_permissions[0]}.']},
					status=status.HTTP_400_BAD_REQUEST,
				)
			permitted_ids = set()
			for permission in permission_filters:
				permitted_ids.update(permitted_domain_ids(request.user, permission))
			domains = domains.filter(id__in=permitted_ids)
		elif getattr(request.user, '_atlas_test_access_active', False):
			domains = domains.filter(id=request.user._atlas_test_domain.id)
		if request.query_params.get('accessible') == 'true' and not request.user.is_superuser:
			membership_domain_ids = DomainMembership.objects.filter(
				user=request.user,
				domain__region__organization=organization,
			).values_list('domain_id', flat=True)
			if membership_domain_ids.exists():
				domains = domains.filter(id__in=membership_domain_ids)

		active_filter = request.query_params.get('active')
		if active_filter in {'true', 'false'}:
			domains = domains.filter(active=active_filter == 'true')

		serializer = DomainSerializer(domains, many=True)
		return Response(serializer.data)

	organization = _organization_from_request(request)
	region_id = request.data.get('region')
	region = get_object_or_404(Region.objects.select_related('organization'), id=region_id) if region_id else None
	if organization is None or region is None or not (
		_is_org_admin(request.user, organization)
		or has_permission(request.user, 'manage_domains', region=region)
	):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	serializer = DomainSerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	region = serializer.validated_data.get('region')
	if region.organization_id != organization.id:
		return Response({'region': ['Region must belong to the selected organization.']}, status=status.HTTP_400_BAD_REQUEST)
	serializer.save()
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def domain_detail(request, domain_id):
	domain = get_object_or_404(Domain.objects.select_related('region__organization'), id=domain_id)
	if not _can_access_organization(request.user, domain.region.organization):
		return Response({'detail': 'Organization access is required.'}, status=status.HTTP_403_FORBIDDEN)

	if request.method == 'GET':
		domain.membership_count = domain.memberships.count()
		serializer = DomainSerializer(domain)
		return Response(serializer.data)
	if not (_is_org_admin(request.user, domain.region.organization) or has_permission(request.user, 'manage_domains', region=domain.region)):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'DELETE':
		if (
			domain.schedule_blocks.exists()
			or domain.schedule_versions.exists()
			or domain.contracts.exists()
			or domain.shared_rules.exists()
		):
			return Response(
				{'detail': 'This domain has scheduling history or rules and must be deactivated instead of deleted.'},
				status=status.HTTP_409_CONFLICT,
			)
		if domain.active and Domain.objects.filter(region__organization=domain.region.organization, active=True).count() <= 1:
			return Response(
				{'detail': 'An organization must retain at least one active domain.'},
				status=status.HTTP_409_CONFLICT,
			)
		domain.delete()
		return Response(status=status.HTTP_204_NO_CONTENT)

	partial = request.method == 'PATCH'
	if request.data.get('active') is False and domain.active and Domain.objects.filter(region__organization=domain.region.organization, active=True).count() <= 1:
		return Response(
			{'detail': 'An organization must retain at least one active domain.'},
			status=status.HTTP_409_CONFLICT,
		)
	serializer = DomainSerializer(domain, data=request.data, partial=partial)
	serializer.is_valid(raise_exception=True)
	serializer.save()
	return Response(serializer.data)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def domain_memberships(request, domain_id):
	domain = get_object_or_404(Domain.objects.select_related('region__organization'), id=domain_id)
	if not _can_access_organization(request.user, domain.region.organization):
		return Response({'detail': 'Organization access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'GET':
		memberships = domain.memberships.select_related('user', 'domain').all()
		return Response(DomainMembershipSerializer(memberships, many=True).data)
	if not (
		_is_org_admin(request.user, domain.region.organization)
		or has_permission(request.user, 'manage_domain_access', domain=domain)
	):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	user_id = request.data.get('user')
	if not user_id:
		return Response({'user': ['This field is required.']}, status=status.HTTP_400_BAD_REQUEST)
	OrganizationMembership.objects.get_or_create(
		organization=domain.region.organization,
		user_id=user_id,
	)
	serializer = DomainMembershipSerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	membership = serializer.save(domain=domain)
	_record_audit(
		request=request,
		organization=domain.region.organization,
		region=domain.region,
		domain=domain,
		target_user=membership.user,
		action='domain_membership.created',
		details={
			'role_template_id': membership.role_template_id,
			'clinically_active': membership.clinically_active,
		},
	)
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def domain_membership_detail(request, membership_id):
	membership = get_object_or_404(
		DomainMembership.objects.select_related('domain__region__organization', 'user'),
		id=membership_id,
	)
	if not (
		_is_org_admin(request.user, membership.domain.region.organization)
		or has_permission(request.user, 'manage_domain_access', domain=membership.domain)
	):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'DELETE':
		if _has_domain_contract_assignment(membership):
			return Response(
				{'detail': 'Remove this user from their Contract in this domain before removing domain access.'},
				status=status.HTTP_409_CONFLICT,
			)
		old_details = {'role_template_id': membership.role_template_id, 'clinically_active': membership.clinically_active}
		membership.delete()
		_record_audit(
			request=request, organization=membership.domain.region.organization,
			region=membership.domain.region, domain=membership.domain,
			target_user=membership.user, action='domain_membership.removed', details=old_details,
		)
		return Response(status=status.HTTP_204_NO_CONTENT)
	if request.data.get('role') == DomainMembership.Role.VIEW_ONLY and _has_domain_contract_assignment(membership):
		return Response(
			{'detail': 'Remove this user from their Contract in this domain before changing them to View Only.'},
			status=status.HTTP_409_CONFLICT,
		)
	serializer = DomainMembershipSerializer(membership, data=request.data, partial=True)
	serializer.is_valid(raise_exception=True)
	old_details = {
		'role_template_id': membership.role_template_id,
		'clinically_active': membership.clinically_active,
		'active': membership.active,
	}
	updated_membership = serializer.save()
	_record_audit(
		request=request, organization=membership.domain.region.organization,
		region=membership.domain.region, domain=membership.domain,
		target_user=membership.user, action='domain_membership.updated',
		details={
			'old': old_details,
			'new': {
				'role_template_id': updated_membership.role_template_id,
				'clinically_active': updated_membership.clinically_active,
				'active': updated_membership.active,
			},
		},
	)
	return Response(serializer.data)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def organization_memberships(request, organization_id):
	organization = get_object_or_404(Organization, id=organization_id)
	if not _is_org_admin(request.user, organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'POST':
		serializer = OrganizationMembershipSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		serializer.save(organization=organization)
		return Response(serializer.data, status=status.HTTP_201_CREATED)
	memberships = organization.memberships.select_related('user').all()
	return Response(OrganizationMembershipSerializer(memberships, many=True).data)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def organization_admins(request, organization_id):
	"""List and add protected organization-wide Org Admin assignments."""
	organization = get_object_or_404(Organization, id=organization_id)
	if not _is_org_admin(request.user, organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)

	active_memberships = organization.memberships.filter(
		active=True,
		user__is_active=True,
		user__physician__active=True,
	).select_related('user', 'user__physician').order_by(
		'user__last_name', 'user__first_name', 'user__email',
	)
	if request.method == 'POST':
		try:
			user_id = int(request.data.get('user_id'))
		except (TypeError, ValueError):
			return Response({'user_id': ['Select an active organization user.']}, status=status.HTTP_400_BAD_REQUEST)
		membership = get_object_or_404(active_memberships, user_id=user_id)
		if membership.is_org_admin:
			return Response({'detail': 'This user is already an Org Admin.'}, status=status.HTTP_409_CONFLICT)
		membership.is_org_admin = True
		membership.save(update_fields=['is_org_admin', 'updated_at'])
		_record_audit(
			request=request,
			organization=organization,
			target_user=membership.user,
			action='organization_admin.assigned',
			details={},
		)

	admins = active_memberships.filter(is_org_admin=True)
	candidates = active_memberships.filter(is_org_admin=False)
	return Response({
		'admins': OrganizationMembershipSerializer(admins, many=True).data,
		'candidates': OrganizationMembershipSerializer(candidates, many=True).data,
	})


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def permission_catalog(request):
	return Response(permission_catalog_payload())


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def role_templates_list_create(request, region_id):
	region = get_object_or_404(Region.objects.select_related('organization'), id=region_id)
	if not _can_access_organization(request.user, region.organization):
		return Response({'detail': 'Organization access is required.'}, status=status.HTTP_403_FORBIDDEN)
	_ensure_region_roles(region)
	if request.method == 'GET':
		if not (
			_is_org_admin(request.user, region.organization)
			or has_permission(request.user, 'view_roles', region=region)
			or has_permission(request.user, 'assign_domain_roles', region=region)
			or has_permission(request.user, 'manage_domain_access', region=region)
		):
			return Response({'detail': 'Role viewing permission is required.'}, status=status.HTTP_403_FORBIDDEN)
		roles = region.role_templates.annotate(assigned_user_count=Count('memberships', distinct=True))
		return Response(RoleTemplateSerializer(roles, many=True).data)
	if not (
		_is_org_admin(request.user, region.organization)
		or has_permission(request.user, 'create_roles', region=region)
	):
		return Response({'detail': 'Role creation permission is required.'}, status=status.HTTP_403_FORBIDDEN)
	serializer = RoleTemplateSerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	role = serializer.save(region=region)
	_record_audit(
		request=request, organization=region.organization, region=region,
		action='role.created', details={'role_id': role.id, 'name': role.name, 'permissions': role.permissions},
	)
	role.assigned_user_count = 0
	return Response(RoleTemplateSerializer(role).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def role_template_detail(request, role_id):
	role = get_object_or_404(RoleTemplate.objects.select_related('region__organization'), id=role_id)
	organization = role.region.organization
	can_view = _is_org_admin(request.user, organization) or has_permission(request.user, 'view_roles', region=role.region)
	if request.method == 'GET':
		if not can_view:
			return Response({'detail': 'Role viewing permission is required.'}, status=status.HTTP_403_FORBIDDEN)
		role.assigned_user_count = role.memberships.count()
		return Response(RoleTemplateSerializer(role).data)
	required_permission = 'delete_unused_roles' if request.method == 'DELETE' else 'edit_roles'
	if not (_is_org_admin(request.user, organization) or has_permission(request.user, required_permission, region=role.region)):
		return Response({'detail': 'Role management permission is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'DELETE':
		if role.memberships.exists():
			return Response({'detail': 'Reassign all users before deleting this Role.'}, status=status.HTTP_409_CONFLICT)
		if role.system_key:
			return Response({'detail': 'Default Roles may be deactivated but not deleted.'}, status=status.HTTP_409_CONFLICT)
		role.delete()
		_record_audit(
			request=request, organization=organization, region=role.region,
			action='role.deleted', details={'role_id': role_id, 'name': role.name},
		)
		return Response(status=status.HTTP_204_NO_CONTENT)
	old = {'name': role.name, 'permissions': role.permissions, 'active': role.active}
	serializer = RoleTemplateSerializer(role, data=request.data, partial=True)
	serializer.is_valid(raise_exception=True)
	updated_role = serializer.save()
	_record_audit(
		request=request, organization=organization, region=role.region,
		action='role.updated', details={
			'role_id': role.id, 'affected_users': role.memberships.values('user_id').distinct().count(),
			'old': old,
			'new': {'name': updated_role.name, 'permissions': updated_role.permissions, 'active': updated_role.active},
		},
	)
	updated_role.assigned_user_count = updated_role.memberships.count()
	return Response(RoleTemplateSerializer(updated_role).data)


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def audit_events(request, organization_id):
	organization = get_object_or_404(Organization, id=organization_id)
	if not (
		_is_org_admin(request.user, organization)
		or has_permission(request.user, 'view_audit_history', organization=organization)
	):
		return Response({'detail': 'Audit permission is required.'}, status=status.HTTP_403_FORBIDDEN)
	events = AuditEvent.objects.filter(organization=organization).select_related(
		'actor', 'target_user', 'region', 'domain',
	)
	region_id = request.query_params.get('region')
	domain_id = request.query_params.get('domain')
	if region_id:
		events = events.filter(region_id=region_id)
	if domain_id:
		events = events.filter(domain_id=domain_id)
	return Response(AuditEventSerializer(events[:500], many=True).data)
