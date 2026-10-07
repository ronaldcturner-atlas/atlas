from collections import OrderedDict

from .models import Domain, DomainMembership, OrganizationMembership


PERMISSION_GROUPS = OrderedDict([
    ('Personal schedule', OrderedDict([
        ('post_own_shifts', 'Post own shifts for pickup'),
        ('offer_own_shift', 'Offer own shift to a specific user'),
        ('propose_trade', 'Propose a shift trade'),
        ('split_own_shift', 'Split and recombine own shifts'),
        ('modify_own_shift_times', 'Modify times of own shifts'),
        ('pick_up_shifts', 'Pick up posted or open shifts'),
        ('submit_own_requests', 'Submit own schedule requests'),
    ])),
    ('Schedule visibility', OrderedDict([
        ('view_published_schedules', 'View published domain schedules'),
        ('view_preview', 'View schedules in Preview'),
        ('view_domain_statistics', 'View domain statistics'),
        ('manage_date_comments', 'Manage schedule date comments'),
    ])),
    ('Published schedule', OrderedDict([
        ('manage_published_assignments', 'Manage published schedule assignments'),
        ('split_any_published_shift', 'Split or recombine any published shift'),
        ('modify_any_published_shift_times', 'Modify times of any published shift'),
        ('manage_any_shift_posting', 'Manage postings for any published shift'),
        ('manage_user_offers_trades', 'Manage shift offers and trades for users'),
        ('approve_pickups_trades', 'Approve shift pickups and trades'),
        ('send_urgent_shift_notifications', 'Send urgent shift notifications'),
        ('batch_update_published_shifts', 'Batch update Shift Builder changes in published schedules'),
    ])),
    ('Build and publication', OrderedDict([
        ('manage_build_workspace', 'Manage Build Workspace'),
        ('administer_requests', 'Review and administer requests in Build Workspace'),
        ('publish_schedule', 'Publish schedules'),
        ('unpublish_schedule', 'Unpublish schedules'),
    ])),
    ('Configuration', OrderedDict([
        ('manage_shift_templates', 'Manage shift templates'),
        ('manage_regional_facilities', 'Manage regional facilities'),
        ('manage_domains', 'Manage domains within a region'),
    ])),
    ('Users', OrderedDict([
        ('view_user_directory', 'View user directory'),
        ('create_users', 'Create users'),
        ('edit_user_profiles', 'Edit user profiles'),
        ('suspend_region_users', 'Suspend or reactivate users within a region'),
        ('manage_domain_access', 'Manage users’ region and domain access'),
        ('change_clinical_status', 'Change clinical activity status'),
        ('assign_domain_roles', 'Assign or change domain roles'),
        ('view_phone_numbers', 'View user phone numbers'),
        ('view_email_addresses', 'View user email addresses'),
    ])),
    ('Roles', OrderedDict([
        ('view_roles', 'View roles and permissions'),
        ('create_roles', 'Create custom roles'),
        ('edit_roles', 'Edit permissions for designated roles'),
        ('activate_roles', 'Activate or deactivate custom roles'),
        ('delete_unused_roles', 'Delete unused custom roles'),
        ('delegate_role_management', 'Delegate role management within a region'),
    ])),
    ('Audit', OrderedDict([
        ('view_audit_history', 'View audit history'),
    ])),
])

ALL_PERMISSIONS = frozenset(
    code for permissions in PERMISSION_GROUPS.values() for code in permissions
)

CLINICAL_PERMISSIONS = frozenset({
    'post_own_shifts', 'offer_own_shift', 'propose_trade', 'split_own_shift',
    'modify_own_shift_times', 'pick_up_shifts', 'submit_own_requests',
})

CLINICAL_DEFAULTS = {
    'view_published_schedules', 'view_preview', 'view_domain_statistics',
    'view_user_directory', 'view_phone_numbers', 'view_email_addresses',
    'post_own_shifts', 'offer_own_shift', 'propose_trade', 'split_own_shift',
    'pick_up_shifts', 'submit_own_requests',
}

SCHEDULER_DEFAULTS = CLINICAL_DEFAULTS | {
    'manage_date_comments', 'manage_published_assignments',
    'split_any_published_shift', 'modify_any_published_shift_times',
    'manage_any_shift_posting', 'manage_user_offers_trades',
    'approve_pickups_trades', 'send_urgent_shift_notifications',
    'batch_update_published_shifts', 'manage_build_workspace',
    'administer_requests', 'publish_schedule', 'unpublish_schedule',
    'manage_shift_templates', 'view_audit_history',
}

DOMAIN_ADMIN_DEFAULTS = SCHEDULER_DEFAULTS | {
    'create_users', 'edit_user_profiles', 'manage_domain_access',
    'change_clinical_status', 'assign_domain_roles',
}

REGIONAL_ADMIN_DEFAULTS = DOMAIN_ADMIN_DEFAULTS | {
    'manage_regional_facilities', 'manage_domains', 'suspend_region_users',
    'view_roles', 'create_roles', 'edit_roles', 'activate_roles',
    'delete_unused_roles', 'delegate_role_management',
}

DEFAULT_ROLE_DEFINITIONS = (
    ('regional_admin', 'Regional Admin', REGIONAL_ADMIN_DEFAULTS),
    ('domain_admin', 'Domain Admin', DOMAIN_ADMIN_DEFAULTS),
    ('scheduler', 'Scheduler', SCHEDULER_DEFAULTS),
    ('medical_director', 'Medical Director', {
        'view_published_schedules', 'view_preview', 'view_domain_statistics',
        'view_user_directory', 'view_phone_numbers', 'view_email_addresses',
        'manage_date_comments',
    } | CLINICAL_DEFAULTS),
    ('staff_physician', 'Staff Physician', CLINICAL_DEFAULTS),
    ('app', 'APP', CLINICAL_DEFAULTS),
    ('view_only', 'View Only', {'view_published_schedules', 'view_preview'}),
)

LEGACY_ROLE_DEFAULTS = {
    'org_admin': ALL_PERMISSIONS,
    'admin': DOMAIN_ADMIN_DEFAULTS,
    'medical_director': CLINICAL_DEFAULTS | {'manage_date_comments'},
    'staff_physician': CLINICAL_DEFAULTS,
    'app': CLINICAL_DEFAULTS,
    'scheduler': SCHEDULER_DEFAULTS,
    'view_only': {'view_published_schedules', 'view_preview'},
}


def is_org_admin(user, organization=None):
    if not user or not user.is_authenticated:
        return False
    if getattr(user, '_atlas_test_access_active', False):
        return False
    if user.is_superuser:
        return True
    memberships = OrganizationMembership.objects.filter(user=user, active=True, is_org_admin=True)
    if organization is not None:
        memberships = memberships.filter(organization=organization)
    if memberships.exists():
        return True
    # Transitional compatibility for records created against the pre-template schema.
    legacy = DomainMembership.objects.filter(user=user, role=DomainMembership.Role.ORG_ADMIN)
    if organization is not None:
        legacy = legacy.filter(domain__region__organization=organization)
    return legacy.exists()


def membership_permissions(membership):
    if not membership or not membership.active:
        return set()
    if membership.role_template_id:
        return set(membership.role_template.permissions or [])
    return set(LEGACY_ROLE_DEFAULTS.get(membership.role, set()))


def has_permission(user, permission, *, domain=None, region=None, organization=None):
    if permission not in ALL_PERMISSIONS or not user or not user.is_authenticated:
        return False
    if getattr(user, '_atlas_test_access_active', False):
        test_domain = user._atlas_test_domain
        if domain is not None and domain.id != test_domain.id:
            return False
        if region is not None and region.id != test_domain.region_id:
            return False
        if organization is not None and organization.id != test_domain.region.organization_id:
            return False
        if domain is None and region is None and organization is None:
            return False
        allowed = permission in set(user._atlas_test_role_template.permissions or [])
        if permission in CLINICAL_PERMISSIONS and not user._atlas_test_clinically_active:
            return False
        return allowed
    if user.is_superuser:
        return True
    target_organization = organization
    if domain is not None:
        target_organization = domain.region.organization
    elif region is not None:
        target_organization = region.organization
    if target_organization is not None and is_org_admin(user, target_organization):
        return True
    memberships = DomainMembership.objects.filter(
        user=user,
        active=True,
        domain__active=True,
        domain__region__active=True,
    ).select_related('role_template', 'domain__region__organization')
    if domain is not None:
        memberships = memberships.filter(domain=domain)
    elif region is not None:
        memberships = memberships.filter(domain__region=region)
    elif organization is not None:
        memberships = memberships.filter(domain__region__organization=organization)
    for membership in memberships:
        if permission in membership_permissions(membership):
            if permission in CLINICAL_PERMISSIONS and not membership.clinically_active:
                continue
            return True
    return False


def is_clinically_active(user, domain):
    if getattr(user, '_atlas_test_access_active', False):
        return bool(
            user._atlas_test_domain.id == domain.id
            and user._atlas_test_clinically_active
        )
    return DomainMembership.objects.filter(
        user=user, domain=domain, active=True, clinically_active=True,
    ).exists()


def permitted_domain_ids(user, permission):
    if not user or not user.is_authenticated:
        return set()
    if getattr(user, '_atlas_test_access_active', False):
        return {
            user._atlas_test_domain.id
        } if has_permission(user, permission, domain=user._atlas_test_domain) else set()
    memberships = DomainMembership.objects.filter(
        user=user, active=True, domain__active=True, domain__region__active=True,
    ).select_related('role_template', 'domain__region__organization')
    organization_ids = set(OrganizationMembership.objects.filter(
        user=user, active=True, is_org_admin=True,
    ).values_list('organization_id', flat=True))
    permitted_ids = {
        membership.domain_id
        for membership in memberships
        if (
            membership.domain.region.organization_id in organization_ids
            or (
                permission in membership_permissions(membership)
                and (permission not in CLINICAL_PERMISSIONS or membership.clinically_active)
            )
        )
    }
    if organization_ids:
        permitted_ids.update(Domain.objects.filter(
            active=True,
            region__active=True,
            region__organization_id__in=organization_ids,
        ).values_list('id', flat=True))
    return permitted_ids
