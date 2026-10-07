from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import status

from .models import Physician
from .serializers import PhysicianSerializer, UserSerializer
from apps.domains.models import Domain, DomainMembership, OrganizationMembership, RoleTemplate
from apps.domains.permissions import has_permission, is_org_admin, permitted_domain_ids


class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return


def _can_use_development_role_test(user):
    return bool(
        settings.DEBUG
        and user.is_authenticated
        and (
            getattr(user, '_atlas_test_actual_org_admin', False)
            or user.is_superuser
            or OrganizationMembership.objects.filter(
                user=user, active=True, is_org_admin=True,
            ).exists()
        )
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """
    Login endpoint. Expects username and password in request body.
    """
    username = str(request.data.get('username', '')).strip().lower()
    password = request.data.get('password')
    
    if not username or not password:
        return Response(
            {'error': 'Username and password are required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    account = User.objects.filter(email__iexact=username).first()
    authentication_username = account.username if account else username
    user = authenticate(
        request,
        username=authentication_username,
        password=password,
    )
    if user is not None:
        login(request, user)
        serializer = UserSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)
    else:
        return Response(
            {'error': 'Invalid username or password'},
            status=status.HTTP_401_UNAUTHORIZED
        )


@api_view(['POST'])
@permission_classes([AllowAny])
def logout_view(request):
    """
    Logout endpoint. Clears the session.
    """
    logout(request)
    return Response({'status': 'logged out'}, status=status.HTTP_200_OK)


# Apply csrf_exempt to logout_view
logout_view = csrf_exempt(logout_view)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me_view(request):
    """
    Get current authenticated user info.
    """
    serializer = UserSerializer(request.user)
    return Response(serializer.data)


@api_view(['GET', 'POST', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def development_role_test(request):
    """Local-only session role simulation for the Atlas developer."""
    if not _can_use_development_role_test(request.user):
        return Response(status=status.HTTP_404_NOT_FOUND)

    if request.method == 'DELETE':
        request.session.pop('atlas_development_role_test', None)
        request.session.modified = True
        return Response({'active': False})

    organization_ids = OrganizationMembership.objects.filter(
        user=request.user, active=True, is_org_admin=True,
    ).values_list('organization_id', flat=True)
    domains = Domain.objects.filter(
        region__organization_id__in=organization_ids,
        active=True,
        region__active=True,
    ).select_related('region__organization').order_by(
        'region__organization__name', 'region__name', 'name',
    )
    if getattr(request.user, '_atlas_test_actual_org_admin', False):
        test_domain = request.user._atlas_test_domain
        organization_ids = [test_domain.region.organization_id]
        domains = Domain.objects.filter(
            region__organization_id__in=organization_ids,
            active=True,
            region__active=True,
        ).select_related('region__organization').order_by(
            'region__organization__name', 'region__name', 'name',
        )

    if request.method == 'GET':
        region_ids = domains.values_list('region_id', flat=True).distinct()
        roles = RoleTemplate.objects.filter(
            region_id__in=region_ids, active=True,
        ).order_by('region__name', 'name')
        return Response({
            'domains': [
                {
                    'id': domain.id,
                    'name': domain.name,
                    'region_id': domain.region_id,
                    'region_name': domain.region.name,
                    'organization_name': domain.region.organization.name,
                }
                for domain in domains
            ],
            'roles': [
                {'id': role.id, 'name': role.name, 'region_id': role.region_id}
                for role in roles
            ],
        })

    domain = get_object_or_404(domains, id=request.data.get('domain_id'))
    role = get_object_or_404(
        RoleTemplate,
        id=request.data.get('role_template_id'),
        region=domain.region,
        active=True,
    )
    request.session['atlas_development_role_test'] = {
        'domain_id': domain.id,
        'role_template_id': role.id,
        'clinically_active': bool(request.data.get('clinically_active')),
    }
    request.session.modified = True
    return Response({'active': True})


def _directory_profile(physician, *, include_email, include_phone, domain=None):
    membership = None
    contract = None
    if domain is not None:
        membership = physician.user.domain_memberships.filter(
            domain=domain, active=True,
        ).select_related('role_template').first()
        assignment = physician.contract_assignments.filter(
            domain=domain,
        ).select_related('contract').first()
        contract = assignment.contract.name if assignment else None
    organization_admin = bool(
        domain
        and OrganizationMembership.objects.filter(
            organization=domain.region.organization,
            user=physician.user,
            active=True,
            is_org_admin=True,
        ).exists()
    )
    return {
        'id': physician.id,
        'name': physician.user.get_full_name() or physician.display_name or physician.user.email,
        'display_name': physician.display_name,
        'email': physician.user.email if include_email else None,
        'phone_number': physician.phone_number if include_phone else None,
        'clinician_type': physician.get_clinician_type_display(),
        'primary_facility': physician.primary_facility.name if physician.primary_facility else None,
        'role_name': (
            membership.role_template.name
            if membership and membership.role_template
            else membership.get_role_display() if membership else None
        ),
        'clinically_active': membership.clinically_active if membership else False,
        'contract': contract,
        'is_org_admin': organization_admin,
    }


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def user_directory(request):
    """Return the signed-in profile and permitted Domain directory entries."""
    self_physician = get_object_or_404(
        Physician.objects.select_related('user', 'primary_facility'),
        user=request.user,
    )
    directory_domain_ids = permitted_domain_ids(request.user, 'view_user_directory')
    domains = Domain.objects.filter(
        id__in=directory_domain_ids, active=True, region__active=True,
    ).select_related('region').order_by('region__name', 'name')

    raw_domain_id = request.query_params.get('domain')
    selected_domain = None
    if raw_domain_id:
        try:
            selected_domain_id = int(raw_domain_id)
        except (TypeError, ValueError):
            return Response({'detail': 'Choose a valid Domain.'}, status=status.HTTP_400_BAD_REQUEST)
        selected_domain = domains.filter(id=selected_domain_id).first()
        if selected_domain is None:
            return Response({'detail': 'You do not have directory access to this Domain.'}, status=status.HTTP_403_FORBIDDEN)
    else:
        selected_domain = domains.first()

    self_memberships = request.user.domain_memberships.filter(active=True).select_related(
        'domain__region', 'role_template',
    )
    if getattr(request.user, '_atlas_test_access_active', False):
        self_memberships = self_memberships.filter(domain=request.user._atlas_test_domain)
    self_profile = _directory_profile(
        self_physician,
        include_email=True,
        include_phone=True,
        domain=selected_domain,
    )
    self_profile['domain_access'] = [
        {
            'domain_id': membership.domain_id,
            'domain_name': membership.domain.name,
            'region_name': membership.domain.region.name,
            'role_name': membership.role_template.name if membership.role_template else membership.get_role_display(),
            'clinically_active': membership.clinically_active,
        }
        for membership in self_memberships
    ]

    users = []
    if selected_domain is not None:
        include_email = has_permission(request.user, 'view_email_addresses', domain=selected_domain)
        include_phone = has_permission(request.user, 'view_phone_numbers', domain=selected_domain)
        directory_user_ids = DomainMembership.objects.filter(
            domain=selected_domain, active=True,
        ).exclude(role_template__system_key='view_only').values('user_id')
        physicians = Physician.objects.filter(
            active=True, user_id__in=directory_user_ids,
        ).select_related('user', 'primary_facility').prefetch_related(
            'user__domain_memberships__role_template',
            'contract_assignments__contract',
        ).distinct().order_by('user__last_name', 'user__first_name', 'id')
        users = [
            _directory_profile(
                physician,
                include_email=include_email or physician.user_id == request.user.id,
                include_phone=include_phone or physician.user_id == request.user.id,
                domain=selected_domain,
            )
            for physician in physicians
        ]

    return Response({
        'self_profile': self_profile,
        'domains': [
            {
                'id': domain.id,
                'name': domain.name,
                'region_id': domain.region_id,
                'region_name': domain.region.name,
            }
            for domain in domains
        ],
        'selected_domain_id': selected_domain.id if selected_domain else None,
        'users': users,
    })


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def physicians_list_create(request):
    if request.method == 'GET':
        physicians = Physician.objects.select_related('user', 'primary_facility').prefetch_related(
            'contract_assignments__contract__domain',
            'user__domain_memberships__domain__region__organization',
            'user__organization_memberships__organization',
        ).all()
        if not request.user.is_superuser:
            organization_ids = OrganizationMembership.objects.filter(
                user=request.user, active=True,
            ).values_list('organization_id', flat=True)
            physicians = physicians.filter(
                user__organization_memberships__organization_id__in=organization_ids,
            ).distinct()
        serializer = PhysicianSerializer(physicians, many=True)
        return Response(serializer.data)

    if not is_org_admin(request.user) and not any(
        has_permission(request.user, 'create_users', domain=membership.domain)
        for membership in DomainMembership.objects.filter(user=request.user, active=True).select_related('domain')
    ):
        return Response({'detail': 'User creation permission is required.'}, status=status.HTTP_403_FORBIDDEN)
    serializer = PhysicianSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def physician_detail(request, physician_id):
    physician = get_object_or_404(
        Physician.objects.select_related('user', 'primary_facility').prefetch_related(
            'contract_assignments__contract__domain',
            'user__domain_memberships__domain__region__organization',
            'user__organization_memberships__organization',
        ),
        id=physician_id,
    )

    if request.method == 'GET':
        serializer = PhysicianSerializer(physician)
        return Response(serializer.data)

    shared_domains = DomainMembership.objects.filter(
        user=physician.user,
        domain_id__in=DomainMembership.objects.filter(user=request.user).values('domain_id'),
    ).select_related('domain')
    if not is_org_admin(request.user) and not any(
        has_permission(request.user, 'edit_user_profiles', domain=membership.domain)
        for membership in shared_domains
    ):
        return Response({'detail': 'User profile editing permission is required.'}, status=status.HTTP_403_FORBIDDEN)
    partial = request.method == 'PATCH'
    serializer = PhysicianSerializer(physician, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def physician_disable(request, physician_id):
    physician = get_object_or_404(Physician, id=physician_id)
    if not is_org_admin(request.user):
        return Response({'detail': 'Org Admin access is required for organization-wide deactivation.'}, status=status.HTTP_403_FORBIDDEN)
    physician.active = False
    physician.save(update_fields=['active'])
    serializer = PhysicianSerializer(physician)
    return Response(serializer.data)
