from django.contrib.auth.models import User
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.scheduling.models import Contract, ContractUserAssignment
from .models import Domain, DomainMembership, Organization, OrganizationMembership, Region, RoleTemplate


class DomainsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='domains-test', password='atlas')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_domains_endpoint_creates_default_physician_domain(self):
        self.assertFalse(Domain.objects.filter(name='Physician').exists())

        response = self.client.get('/api/domains/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Domain.objects.filter(name='Physician', active=True).exists())

    @override_settings(ATLAS_ALLOW_SELF_SERVICE_ORGANIZATION_BOOTSTRAP=False)
    def test_empty_production_database_cannot_be_claimed_by_first_user(self):
        Organization.objects.all().delete()
        response = self.client.get('/api/domains/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertFalse(Organization.objects.exists())

        create = self.client.post(
            '/api/organizations/',
            {'name': 'Claimed Organization'},
            format='json',
        )

        self.assertEqual(create.status_code, 403)
        self.assertFalse(Organization.objects.exists())

    def test_domain_region_cannot_be_changed_after_creation(self):
        organization = Organization.objects.create(name='Immutable Domain Organization')
        first_region = Region.objects.create(organization=organization, name='First Region')
        second_region = Region.objects.create(organization=organization, name='Second Region')
        domain = Domain.objects.create(region=first_region, name='Physician')
        OrganizationMembership.objects.create(
            organization=organization,
            user=self.user,
            is_org_admin=True,
        )

        response = self.client.patch(
            f'/api/domains/{domain.id}/',
            {'region': second_region.id},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        domain.refresh_from_db()
        self.assertEqual(domain.region_id, first_region.id)

    def test_domain_membership_identity_cannot_be_retargeted(self):
        organization = Organization.objects.create(name='Immutable Membership Organization')
        region = Region.objects.create(organization=organization, name='Membership Region')
        first_domain = Domain.objects.create(region=region, name='Physician')
        second_domain = Domain.objects.create(region=region, name='APP')
        OrganizationMembership.objects.create(
            organization=organization,
            user=self.user,
            is_org_admin=True,
        )
        first_member = User.objects.create_user('first-member')
        second_member = User.objects.create_user('second-member')
        OrganizationMembership.objects.create(organization=organization, user=first_member)
        OrganizationMembership.objects.create(organization=organization, user=second_member)
        membership = DomainMembership.objects.create(
            domain=first_domain,
            user=first_member,
            role=DomainMembership.Role.VIEW_ONLY,
        )

        move_domain = self.client.patch(
            f'/api/domain-memberships/{membership.id}/',
            {'domain': second_domain.id},
            format='json',
        )
        move_user = self.client.patch(
            f'/api/domain-memberships/{membership.id}/',
            {'user': second_member.id},
            format='json',
        )

        self.assertEqual(move_domain.status_code, 400)
        self.assertEqual(move_user.status_code, 400)
        membership.refresh_from_db()
        self.assertEqual(membership.domain_id, first_domain.id)
        self.assertEqual(membership.user_id, first_member.id)

    def test_permission_catalog_requires_role_management_access(self):
        denied = self.client.get('/api/permissions/catalog/')
        self.assertEqual(denied.status_code, 403)

        organization = Organization.objects.create(name='Permission Catalog Organization')
        region = Region.objects.create(organization=organization, name='Permission Catalog Region')
        domain = Domain.objects.create(region=region, name='Physician')
        role = RoleTemplate.objects.create(
            region=region,
            name='Role Viewer',
            permissions=['view_roles'],
        )
        OrganizationMembership.objects.create(organization=organization, user=self.user)
        DomainMembership.objects.create(
            domain=domain,
            user=self.user,
            role=DomainMembership.Role.ADMIN,
            role_template=role,
            clinically_active=False,
        )

        allowed = self.client.get('/api/permissions/catalog/')
        self.assertEqual(allowed.status_code, 200)

    def test_domains_endpoint_active_filter(self):
        Domain.objects.create(name='Emergency Medicine', active=True)
        Domain.objects.create(name='Critical Care', active=False)

        response = self.client.get('/api/domains/?active=true')

        self.assertEqual(response.status_code, 200)
        returned_names = {item['name'] for item in response.json()}
        self.assertIn('Emergency Medicine', returned_names)
        self.assertIn('Physician', returned_names)
        self.assertNotIn('Critical Care', returned_names)

    def test_accessible_domains_include_view_only_memberships(self):
        organization = Organization.objects.create(name='Schedule Access Organization')
        region = Region.objects.create(organization=organization, name='Schedule Access Region')
        visible_domain = Domain.objects.create(region=region, name='Visible APP Schedule')
        Domain.objects.create(region=region, name='Hidden Schedule')
        OrganizationMembership.objects.create(organization=organization, user=self.user)
        DomainMembership.objects.create(
            domain=visible_domain,
            user=self.user,
            role=DomainMembership.Role.VIEW_ONLY,
        )

        response = self.client.get(
            f'/api/domains/?organization={organization.id}&active=true&accessible=true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.json()], [visible_domain.id])

    def test_accessible_domains_include_every_domain_for_org_admin(self):
        organization = Organization.objects.create(name='Org Admin Schedule Access')
        region = Region.objects.create(organization=organization, name='Org Admin Region')
        first_domain = Domain.objects.create(region=region, name='Physician')
        second_domain = Domain.objects.create(region=region, name='APP')
        OrganizationMembership.objects.create(
            organization=organization,
            user=self.user,
            is_org_admin=True,
        )

        response = self.client.get(
            f'/api/domains/?organization={organization.id}&active=true&accessible=true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item['id'] for item in response.json()},
            {first_domain.id, second_domain.id},
        )

    def test_org_admin_can_manage_domains_and_domain_roles(self):
        organization, _ = Organization.objects.get_or_create(name='Lowcountry Emergency Physicians')
        region, _ = Region.objects.get_or_create(organization=organization, name='Lowcountry Emergency Physicians')
        physician_domain = Domain.objects.create(region=region, name='Physicians')
        OrganizationMembership.objects.create(
            organization=organization,
            user=self.user,
        )
        DomainMembership.objects.create(
            domain=physician_domain,
            user=self.user,
            role=DomainMembership.Role.ORG_ADMIN,
        )

        create_domain = self.client.post(
            '/api/domains/',
            {'organization': organization.id, 'region': region.id, 'name': 'APPs'},
            format='json',
        )
        self.assertEqual(create_domain.status_code, 201)
        app_domain_id = create_domain.json()['id']

        rename_domain = self.client.patch(
            f'/api/domains/{app_domain_id}/',
            {'name': 'Advanced Practice Providers'},
            format='json',
        )
        self.assertEqual(rename_domain.status_code, 200)
        self.assertEqual(rename_domain.json()['name'], 'Advanced Practice Providers')

        worker = User.objects.create_user(
            username='worker@example.com',
            email='worker@example.com',
            first_name='Alex',
            last_name='Morgan',
        )
        OrganizationMembership.objects.create(organization=organization, user=worker)
        physician_membership = self.client.post(
            f'/api/domains/{physician_domain.id}/memberships/',
            {'user': worker.id, 'role': 'staff_physician'},
            format='json',
        )
        app_membership = self.client.post(
            f'/api/domains/{app_domain_id}/memberships/',
            {'user': worker.id, 'role': 'view_only'},
            format='json',
        )
        self.assertEqual(physician_membership.status_code, 201)
        self.assertEqual(app_membership.status_code, 201)
        self.assertEqual(
            set(DomainMembership.objects.filter(user=worker).values_list('role', flat=True)),
            {'staff_physician', 'view_only'},
        )

        physician = Physician.objects.create(user=worker)
        contract = Contract.objects.create(domain=physician_domain, name='Physician Contract')
        ContractUserAssignment.objects.create(
            contract=contract,
            domain=physician_domain,
            physician=physician,
        )
        blocked_view_only = self.client.patch(
            f"/api/domain-memberships/{physician_membership.json()['id']}/",
            {'role': 'view_only'},
            format='json',
        )
        self.assertEqual(blocked_view_only.status_code, 409)

    def test_non_org_admin_cannot_change_domains(self):
        organization = Organization.objects.create(name='Member Organization')
        region = Region.objects.create(organization=organization, name='Member Organization')
        OrganizationMembership.objects.create(
            organization=organization,
            user=self.user,
        )
        domain = Domain.objects.create(region=region, name='Physicians')
        DomainMembership.objects.create(
            domain=domain,
            user=self.user,
            role=DomainMembership.Role.VIEW_ONLY,
        )

        response = self.client.patch(
            f'/api/domains/{domain.id}/',
            {'name': 'Renamed'},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        domain.refresh_from_db()
        self.assertEqual(domain.name, 'Physicians')

    def test_org_admin_can_manage_regions(self):
        organization = Organization.objects.create(name='Regional Organization')
        region = Region.objects.create(organization=organization, name='Region A')
        domain = Domain.objects.create(region=region, name='Physician')
        OrganizationMembership.objects.create(organization=organization, user=self.user)
        DomainMembership.objects.create(domain=domain, user=self.user, role=DomainMembership.Role.ORG_ADMIN)

        response = self.client.post(
            f'/api/organizations/{organization.id}/regions/',
            {'name': 'Region B'},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['name'], 'Region B')

        regions = self.client.get(f'/api/organizations/{organization.id}/regions/')
        self.assertEqual(regions.status_code, 200)
        self.assertEqual({item['name'] for item in regions.json()}, {'Region A', 'Region B'})

    def test_domain_membership_management_requires_existing_organization_user_and_field_permissions(self):
        organization = Organization.objects.create(name='Delegated Membership Organization')
        region = Region.objects.create(organization=organization, name='Delegated Region')
        domain = Domain.objects.create(region=region, name='Delegated Domain')
        manager_role = RoleTemplate.objects.create(
            region=region,
            name='Access Manager',
            permissions=['manage_domain_access'],
        )
        staff_role = RoleTemplate.objects.create(
            region=region,
            name='Staff',
            permissions=['view_published_schedules'],
        )
        OrganizationMembership.objects.create(organization=organization, user=self.user)
        DomainMembership.objects.create(
            domain=domain,
            user=self.user,
            role=DomainMembership.Role.ADMIN,
            role_template=manager_role,
            clinically_active=False,
        )
        organization_user = User.objects.create_user('organization-user')
        OrganizationMembership.objects.create(organization=organization, user=organization_user)
        outsider = User.objects.create_user('membership-outsider')

        outsider_response = self.client.post(
            f'/api/domains/{domain.id}/memberships/',
            {'user': outsider.id, 'role_template': staff_role.id, 'clinically_active': False},
            format='json',
        )
        missing_role_permission = self.client.post(
            f'/api/domains/{domain.id}/memberships/',
            {'user': organization_user.id, 'role_template': staff_role.id, 'clinically_active': False},
            format='json',
        )

        self.assertEqual(outsider_response.status_code, 400)
        self.assertFalse(OrganizationMembership.objects.filter(organization=organization, user=outsider).exists())
        self.assertEqual(missing_role_permission.status_code, 403)

    def test_delegated_role_manager_cannot_grant_permissions_they_do_not_hold(self):
        organization = Organization.objects.create(name='Role Ceiling Organization')
        region = Region.objects.create(organization=organization, name='Role Ceiling Region')
        domain = Domain.objects.create(region=region, name='Role Ceiling Domain')
        manager_role = RoleTemplate.objects.create(
            region=region,
            name='Limited Role Manager',
            permissions=['view_roles', 'create_roles', 'edit_roles'],
        )
        OrganizationMembership.objects.create(organization=organization, user=self.user)
        DomainMembership.objects.create(
            domain=domain,
            user=self.user,
            role=DomainMembership.Role.ADMIN,
            role_template=manager_role,
            clinically_active=False,
        )

        denied = self.client.post(
            f'/api/regions/{region.id}/roles/',
            {'name': 'Escalated Role', 'permissions': ['manage_domains']},
            format='json',
        )
        allowed = self.client.post(
            f'/api/regions/{region.id}/roles/',
            {'name': 'Limited Child Role', 'permissions': ['view_roles']},
            format='json',
        )

        self.assertEqual(denied.status_code, 403, denied.json())
        self.assertEqual(allowed.status_code, 201)

    def test_unused_custom_role_must_age_one_year_before_deletion(self):
        organization = Organization.objects.create(name='Role Retention Organization')
        region = Region.objects.create(organization=organization, name='Role Retention Region')
        OrganizationMembership.objects.create(
            organization=organization,
            user=self.user,
            is_org_admin=True,
        )
        recent_role = RoleTemplate.objects.create(region=region, name='Recent Custom Role')
        old_role = RoleTemplate.objects.create(
            region=region,
            name='Old Custom Role',
            last_unassigned_at=timezone.now() - timedelta(days=366),
        )

        recent_response = self.client.delete(f'/api/roles/{recent_role.id}/')
        old_response = self.client.delete(f'/api/roles/{old_role.id}/')

        self.assertEqual(recent_response.status_code, 409)
        self.assertEqual(old_response.status_code, 204)
