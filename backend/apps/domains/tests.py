from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.scheduling.models import Contract, ContractUserAssignment
from .models import Domain, DomainMembership, Organization, OrganizationMembership, Region


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

    def test_domains_endpoint_active_filter(self):
        Domain.objects.create(name='Emergency Medicine', active=True)
        Domain.objects.create(name='Critical Care', active=False)

        response = self.client.get('/api/domains/?active=true')

        self.assertEqual(response.status_code, 200)
        returned_names = {item['name'] for item in response.json()}
        self.assertIn('Emergency Medicine', returned_names)
        self.assertIn('Physician', returned_names)
        self.assertNotIn('Critical Care', returned_names)

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
