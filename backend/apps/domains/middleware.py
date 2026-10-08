from django.conf import settings

from .models import Domain, OrganizationMembership, RoleTemplate


class DevelopmentRoleTestMiddleware:
    """Apply a session-only role override for local permission testing."""

    session_key = 'atlas_development_role_test'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.ATLAS_ENABLE_DEVELOPMENT_ROLE_TEST and request.user.is_authenticated:
            context = request.session.get(self.session_key)
            if context:
                actual_org_admin = bool(
                    request.user.is_superuser
                    or OrganizationMembership.objects.filter(
                        user=request.user,
                        active=True,
                        is_org_admin=True,
                    ).exists()
                )
                if actual_org_admin:
                    domain = Domain.objects.filter(
                        id=context.get('domain_id'),
                        active=True,
                        region__active=True,
                    ).select_related('region__organization').first()
                    role = RoleTemplate.objects.filter(
                        id=context.get('role_template_id'),
                        active=True,
                    ).first()
                    if domain and role and role.region_id == domain.region_id:
                        request.user._atlas_test_access_active = True
                        request.user._atlas_test_actual_org_admin = True
                        request.user._atlas_test_domain = domain
                        request.user._atlas_test_role_template = role
                        request.user._atlas_test_clinically_active = bool(context.get('clinically_active'))
                        request.user.is_staff = False
                        request.user.is_superuser = False
                    else:
                        request.session.pop(self.session_key, None)
        return self.get_response(request)
