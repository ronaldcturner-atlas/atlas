from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.middleware.csrf import get_token
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework import status

from .models import AccountSecurityState, Physician
from .security import issue_temporary_password
from .serializers import PhysicianSerializer, UserSerializer
from .throttles import LoginAccountThrottle, LoginIPThrottle
from apps.domains.models import Domain, DomainMembership, Organization, OrganizationMembership, RoleTemplate
from apps.domains.permissions import has_permission, is_org_admin, permitted_domain_ids
from apps.facilities.models import Facility


class CsrfProtectedSessionAuthentication(SessionAuthentication):
    """Session authentication with Django REST Framework's CSRF enforcement."""


class CsrfRequired(BasePermission):
    """Require a valid CSRF token even before a user has authenticated."""

    def has_permission(self, request, view):
        SessionAuthentication().enforce_csrf(request)
        return True


@ensure_csrf_cookie
@api_view(['GET'])
@permission_classes([AllowAny])
def csrf_view(request):
    return Response({'csrfToken': get_token(request)})


def _can_use_development_role_test(user):
    return bool(
        settings.ATLAS_ENABLE_DEVELOPMENT_ROLE_TEST
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
@permission_classes([AllowAny, CsrfRequired])
@throttle_classes([LoginIPThrottle, LoginAccountThrottle])
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
    if settings.REJECT_SHARED_TEST_PASSWORD and password == 'atlas':
        return Response(
            {'error': 'Invalid username or password'},
            status=status.HTTP_401_UNAUTHORIZED,
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
        payload = dict(UserSerializer(user).data)
        payload['csrfToken'] = get_token(request)
        return Response(payload, status=status.HTTP_200_OK)
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


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me_view(request):
    """
    Get current authenticated user info.
    """
    serializer = UserSerializer(request.user)
    return Response(serializer.data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def change_password(request):
    current_password = request.data.get('current_password')
    new_password = request.data.get('new_password')
    confirm_password = request.data.get('confirm_password')
    if not current_password or not new_password or not confirm_password:
        return Response(
            {'detail': 'Current password, new password, and confirmation are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not request.user.check_password(current_password):
        return Response(
            {'current_password': ['Current password is incorrect.']},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if new_password != confirm_password:
        return Response(
            {'confirm_password': ['New passwords do not match.']},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if request.user.check_password(new_password):
        return Response(
            {'new_password': ['Choose a password you have not already been using.']},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        validate_password(new_password, user=request.user)
    except ValidationError as exc:
        return Response(
            {'new_password': list(exc.messages)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    request.user.set_password(new_password)
    request.user.save(update_fields=['password'])
    state, _created = AccountSecurityState.objects.get_or_create(
        user=request.user,
    )
    state.mark_password_changed()
    update_session_auth_hash(request, request.user)
    return Response(dict(UserSerializer(request.user).data))


@api_view(['GET', 'POST', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
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


def _physician_organization_ids(physician):
    return {
        membership.organization_id
        for membership in physician.user.organization_memberships.all()
        if membership.active
    }


def _visible_physician_domain_ids(user, physician):
    target_domain_ids = {
        membership.domain_id
        for membership in physician.user.domain_memberships.all()
        if membership.active
    }
    if user.id == physician.user_id:
        return target_domain_ids
    return target_domain_ids & permitted_domain_ids(user, 'view_user_directory')


def _serialize_physician_for_user(
    physician,
    user,
    *,
    visible_domain_ids=None,
    visible_organization_ids=None,
    email_domain_ids=None,
    phone_domain_ids=None,
):
    if visible_domain_ids is None:
        visible_domain_ids = _visible_physician_domain_ids(user, physician)
    visible_domain_ids = set(visible_domain_ids)
    data = dict(PhysicianSerializer(physician).data)
    data['domain_memberships'] = [
        membership for membership in data['domain_memberships']
        if membership['domain_id'] in visible_domain_ids
    ]
    data['current_contracts'] = [
        contract for contract in data['current_contracts']
        if contract['domain_id'] in visible_domain_ids
    ]
    if visible_organization_ids is None:
        visible_organization_ids = set(Domain.objects.filter(
            id__in=visible_domain_ids,
        ).values_list('region__organization_id', flat=True))
    else:
        visible_organization_ids = set(visible_organization_ids)
    data['organization_memberships'] = [
        membership for membership in data['organization_memberships']
        if membership['organization_id'] in visible_organization_ids
    ]
    if user.id != physician.user_id:
        if email_domain_ids is None:
            email_domain_ids = permitted_domain_ids(user, 'view_email_addresses')
        if phone_domain_ids is None:
            phone_domain_ids = permitted_domain_ids(user, 'view_phone_numbers')
        if not visible_domain_ids.intersection(email_domain_ids):
            data['email'] = None
        if not visible_domain_ids.intersection(phone_domain_ids):
            data['phone_number'] = None
    return data


def _user_creation_organization_ids(user):
    if user.is_superuser:
        return set(Organization.objects.values_list('id', flat=True))
    organization_ids = set(OrganizationMembership.objects.filter(
        user=user,
        active=True,
        is_org_admin=True,
    ).values_list('organization_id', flat=True))
    organization_ids.update(Domain.objects.filter(
        id__in=permitted_domain_ids(user, 'create_users'),
    ).values_list('region__organization_id', flat=True))
    return organization_ids


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
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
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def physicians_list_create(request):
    if request.method == 'GET':
        physicians = Physician.objects.select_related('user', 'primary_facility').prefetch_related(
            'contract_assignments__contract__domain',
            'user__domain_memberships__domain__region__organization',
            'user__domain_memberships__role_template',
            'user__organization_memberships__organization',
        ).all()
        if request.user.is_superuser:
            visible_domain_ids = set(Domain.objects.values_list('id', flat=True))
        else:
            visible_domain_ids = permitted_domain_ids(request.user, 'view_user_directory')
            physicians = physicians.filter(
                user__domain_memberships__domain_id__in=visible_domain_ids,
                user__domain_memberships__active=True,
            ).distinct()
            own_physician_id = getattr(getattr(request.user, 'physician', None), 'id', None)
            if own_physician_id:
                physicians = Physician.objects.filter(
                    Q(id__in=physicians.values('id')) | Q(id=own_physician_id)
                ).select_related('user', 'primary_facility').prefetch_related(
                    'contract_assignments__contract__domain',
                    'user__domain_memberships__domain__region__organization',
                    'user__domain_memberships__role_template',
                    'user__organization_memberships__organization',
                ).distinct()
        email_domain_ids = permitted_domain_ids(request.user, 'view_email_addresses')
        phone_domain_ids = permitted_domain_ids(request.user, 'view_phone_numbers')
        return Response([
            _serialize_physician_for_user(physician, request.user, visible_domain_ids=(
                _visible_physician_domain_ids(request.user, physician)
                if not request.user.is_superuser else visible_domain_ids
            ), email_domain_ids=email_domain_ids, phone_domain_ids=phone_domain_ids)
            for physician in physicians
        ])

    allowed_organization_ids = _user_creation_organization_ids(request.user)
    if not allowed_organization_ids:
        return Response({'detail': 'User creation permission is required.'}, status=status.HTTP_403_FORBIDDEN)
    raw_organization_id = request.data.get('organization')
    if raw_organization_id in (None, ''):
        if len(allowed_organization_ids) != 1:
            return Response(
                {'organization': ['Select the organization for this user.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        organization_id = next(iter(allowed_organization_ids))
    else:
        try:
            organization_id = int(raw_organization_id)
        except (TypeError, ValueError):
            return Response({'organization': ['Select a valid organization.']}, status=status.HTTP_400_BAD_REQUEST)
        if organization_id not in allowed_organization_ids:
            return Response({'detail': 'User creation permission is required for this organization.'}, status=status.HTTP_403_FORBIDDEN)
    serializer_data = request.data.copy()
    serializer_data.pop('organization', None)
    serializer = PhysicianSerializer(data=serializer_data)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        physician = serializer.save()
        temporary_password = issue_temporary_password(physician.user)
        OrganizationMembership.objects.create(
            organization_id=organization_id,
            user=physician.user,
        )
    response_data = _serialize_physician_for_user(
            physician,
            request.user,
            visible_domain_ids=set(),
            visible_organization_ids={organization_id},
        )
    response_data['temporary_password'] = temporary_password
    return Response(response_data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def physician_detail(request, physician_id):
    physician = get_object_or_404(
        Physician.objects.select_related('user', 'primary_facility').prefetch_related(
            'contract_assignments__contract__domain',
            'user__domain_memberships__domain__region__organization',
            'user__domain_memberships__role_template',
            'user__organization_memberships__organization',
        ),
        id=physician_id,
    )

    target_organization_ids = _physician_organization_ids(physician)
    administered_organization_ids = {
        organization_id
        for organization_id in target_organization_ids
        if is_org_admin(request.user, Organization.objects.get(id=organization_id))
    }
    has_global_access = request.user.is_superuser
    visible_domain_ids = _visible_physician_domain_ids(request.user, physician)
    if administered_organization_ids:
        visible_domain_ids.update(Domain.objects.filter(
            region__organization_id__in=administered_organization_ids,
        ).values_list('id', flat=True))
    if has_global_access:
        visible_domain_ids = set(DomainMembership.objects.filter(
            user=physician.user,
        ).values_list('domain_id', flat=True))
    visible_organization_ids = set(Domain.objects.filter(
        id__in=visible_domain_ids,
    ).values_list('region__organization_id', flat=True)) | administered_organization_ids
    if request.method == 'GET':
        if request.user.id != physician.user_id and not visible_domain_ids and not administered_organization_ids and not has_global_access:
            return Response({'detail': 'User directory access is required.'}, status=status.HTTP_403_FORBIDDEN)
        return Response(_serialize_physician_for_user(
            physician,
            request.user,
            visible_domain_ids=visible_domain_ids,
            visible_organization_ids=visible_organization_ids,
        ))

    shared_domains = DomainMembership.objects.filter(
        user=physician.user,
        active=True,
        domain_id__in=permitted_domain_ids(request.user, 'edit_user_profiles'),
    ).select_related('domain__region')
    is_self = request.user.id == physician.user_id
    can_administer_profile = bool(
        has_global_access
        or (
            target_organization_ids
            and target_organization_ids.issubset(administered_organization_ids)
        )
    )
    editable_organization_ids = administered_organization_ids | set(
        shared_domains.values_list(
            'domain__region__organization_id', flat=True,
        )
    )
    if (
        not is_self
        and not has_global_access
        and (
            not editable_organization_ids
            or not target_organization_ids
            or not target_organization_ids.issubset(editable_organization_ids)
        )
    ):
        return Response({
            'detail': (
                'User profile editing permission is required in every '
                'Organization assigned to this user.'
            ),
        }, status=status.HTTP_403_FORBIDDEN)
    serializer_data = request.data.copy()
    if is_self and not can_administer_profile:
        allowed_fields = {'first_name', 'last_name', 'email', 'phone_number'}
        disallowed = set(serializer_data) - allowed_fields
        if disallowed:
            return Response({'detail': 'You may only update your own contact profile.'}, status=status.HTTP_403_FORBIDDEN)
        if (
            physician.phone_number
            and 'phone_number' in serializer_data
            and not str(serializer_data.get('phone_number', '')).strip()
        ):
            return Response({'phone_number': ['Phone number cannot be left blank once added.']}, status=status.HTTP_400_BAD_REQUEST)
    else:
        serializer_data.pop('role', None)
        if 'active' in serializer_data:
            requested_active = serializer_data.pop('active')
            if requested_active != physician.active:
                return Response(
                    {'detail': 'Use the protected user deactivation workflow to change account status.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        if serializer_data.get('primary_facility') not in (None, ''):
            facility = get_object_or_404(Facility.objects.select_related('region'), id=serializer_data['primary_facility'])
            allowed_region_ids = set(shared_domains.values_list('domain__region_id', flat=True))
            allowed_region_ids.update(Facility.objects.filter(
                region__organization_id__in=administered_organization_ids,
            ).values_list('region_id', flat=True))
            if facility.region_id not in allowed_region_ids:
                return Response(
                    {'primary_facility': ['Select a facility within a Region you manage for this user.']},
                    status=status.HTTP_403_FORBIDDEN,
                )
    partial = request.method == 'PATCH'
    serializer = PhysicianSerializer(physician, data=serializer_data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(_serialize_physician_for_user(
        physician,
        request.user,
        visible_domain_ids=visible_domain_ids,
        visible_organization_ids=visible_organization_ids,
    ))


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def physician_disable(request, physician_id):
    physician = get_object_or_404(Physician.objects.select_related('user'), id=physician_id)
    organization_ids = _physician_organization_ids(physician)
    if not organization_ids or any(
        not is_org_admin(request.user, organization)
        for organization in Organization.objects.filter(id__in=organization_ids)
    ):
        return Response({'detail': 'Org Admin access is required for organization-wide deactivation.'}, status=status.HTTP_403_FORBIDDEN)
    protected_organization = OrganizationMembership.objects.filter(
        organization_id__in=organization_ids,
        user=physician.user,
        active=True,
        is_org_admin=True,
    ).first()
    if protected_organization and not OrganizationMembership.objects.filter(
        organization=protected_organization.organization,
        active=True,
        is_org_admin=True,
        user__is_active=True,
        user__physician__active=True,
    ).exclude(user=physician.user).exists():
        return Response(
            {'detail': 'Assign another active Org Admin before deactivating this user.'},
            status=status.HTTP_409_CONFLICT,
        )
    physician.active = False
    physician.save(update_fields=['active'])
    physician.user.is_active = False
    physician.user.save(update_fields=['is_active'])
    return Response(_serialize_physician_for_user(physician, request.user))


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def physician_reset_password(request, physician_id):
    physician = get_object_or_404(
        Physician.objects.select_related('user'),
        id=physician_id,
    )
    if physician.user_id == request.user.id:
        return Response(
            {'detail': 'Use Change Password to update your own password.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not physician.active or not physician.user.is_active:
        return Response(
            {'detail': 'Reactivate this user before resetting their password.'},
            status=status.HTTP_409_CONFLICT,
        )
    organization_ids = _physician_organization_ids(physician)
    if not organization_ids:
        return Response(
            {'detail': 'This user is not assigned to an organization.'},
            status=status.HTTP_409_CONFLICT,
        )
    if not request.user.is_superuser and any(
        not is_org_admin(request.user, organization)
        for organization in Organization.objects.filter(id__in=organization_ids)
    ):
        return Response(
            {'detail': 'Org Admin access is required for every organization assigned to this user.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    temporary_password = issue_temporary_password(physician.user)
    return Response({
        'temporary_password': temporary_password,
        'must_change_password': True,
        'user_id': physician.user_id,
    })
