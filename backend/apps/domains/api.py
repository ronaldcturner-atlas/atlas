from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Domain, DomainMembership, Organization, OrganizationMembership, Region
from .serializers import (
	DomainMembershipSerializer,
	DomainSerializer,
	OrganizationMembershipSerializer,
	OrganizationSerializer,
	RegionSerializer,
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
	return user.is_superuser or DomainMembership.objects.filter(
		domain__region__organization=organization,
		user=user,
		role=DomainMembership.Role.ORG_ADMIN,
	).exists()


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
		DomainMembership.objects.create(
			domain=default_domain,
			user=request.user,
			role=DomainMembership.Role.ORG_ADMIN,
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
	OrganizationMembership.objects.create(organization=organization, user=request.user)
	default_region = Region.objects.create(organization=organization, name=organization.name)
	default_domain = Domain.objects.create(region=default_region, name='Physician')
	DomainMembership.objects.create(
		domain=default_domain,
		user=request.user,
		role=DomainMembership.Role.ORG_ADMIN,
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
		return Response(RegionSerializer(regions, many=True).data)
	if not _is_org_admin(request.user, organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	serializer = RegionSerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	serializer.save(organization=organization)
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def region_detail(request, region_id):
	region = get_object_or_404(Region.objects.select_related('organization'), id=region_id)
	organization = region.organization
	if not _can_access_organization(request.user, organization):
		return Response({'detail': 'Organization access is required.'}, status=status.HTTP_403_FORBIDDEN)
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

		active_filter = request.query_params.get('active')
		if active_filter in {'true', 'false'}:
			domains = domains.filter(active=active_filter == 'true')

		serializer = DomainSerializer(domains, many=True)
		return Response(serializer.data)

	organization = _organization_from_request(request)
	if organization is None or not _is_org_admin(request.user, organization):
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
	if not _is_org_admin(request.user, domain.region.organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	if request.method == 'DELETE':
		if domain.schedule_versions.exists() or domain.contracts.exists() or domain.shared_rules.exists():
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
	if not _is_org_admin(request.user, domain.region.organization):
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
	serializer.save(domain=domain)
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def domain_membership_detail(request, membership_id):
	membership = get_object_or_404(
		DomainMembership.objects.select_related('domain__region__organization', 'user'),
		id=membership_id,
	)
	if not _is_org_admin(request.user, membership.domain.region.organization):
		return Response({'detail': 'Org Admin access is required.'}, status=status.HTTP_403_FORBIDDEN)
	is_removing_org_admin = (
		membership.role == DomainMembership.Role.ORG_ADMIN
		and (
			request.method == 'DELETE'
			or request.data.get('role') not in {None, DomainMembership.Role.ORG_ADMIN}
		)
	)
	if is_removing_org_admin and DomainMembership.objects.filter(
		domain__region__organization=membership.domain.region.organization,
		role=DomainMembership.Role.ORG_ADMIN,
	).count() <= 1:
		return Response(
			{'detail': 'An organization must retain at least one Org Admin role in a domain.'},
			status=status.HTTP_409_CONFLICT,
		)
	if request.method == 'DELETE':
		if _has_domain_contract_assignment(membership):
			return Response(
				{'detail': 'Remove this user from their Contract in this domain before removing domain access.'},
				status=status.HTTP_409_CONFLICT,
			)
		membership.delete()
		return Response(status=status.HTTP_204_NO_CONTENT)
	if request.data.get('role') == DomainMembership.Role.VIEW_ONLY and _has_domain_contract_assignment(membership):
		return Response(
			{'detail': 'Remove this user from their Contract in this domain before changing them to View Only.'},
			status=status.HTTP_409_CONFLICT,
		)
	serializer = DomainMembershipSerializer(membership, data=request.data, partial=True)
	serializer.is_valid(raise_exception=True)
	serializer.save()
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
