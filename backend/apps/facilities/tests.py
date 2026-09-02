from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Facility


class FacilitiesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='facilities-test', password='atlas')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_short_name_is_required(self):
        facility = Facility(name='Berkeley Hospital', short_name='')

        with self.assertRaises(ValidationError):
            facility.full_clean()

    def test_facility_patch_updates_short_name_and_active_state(self):
        facility = Facility.objects.create(
            name='Berkeley Hospital',
            short_name='Berkeley',
            timezone='UTC',
            color='#2563eb',
            active=True,
        )

        response = self.client.patch(
            f'/api/facilities/{facility.id}/',
            {'short_name': 'Berkeley', 'active': False},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        facility.refresh_from_db()
        self.assertEqual(facility.short_name, 'Berkeley')
        self.assertFalse(facility.active)

        response = self.client.patch(
            f'/api/facilities/{facility.id}/',
            {'active': True},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        facility.refresh_from_db()
        self.assertTrue(facility.active)

    def test_facilities_list_active_filter(self):
        Facility.objects.create(name='Alpha', short_name='Alpha', active=True)
        Facility.objects.create(name='Beta', short_name='Beta', active=False)

        response = self.client.get('/api/facilities/?active=true')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        names = {item['name'] for item in payload}
        self.assertIn('Alpha', names)
        self.assertNotIn('Beta', names)

    def test_facilities_can_be_reordered(self):
        alpha = Facility.objects.create(name='Alpha', short_name='Alpha', sort_order=1)
        beta = Facility.objects.create(name='Beta', short_name='Beta', sort_order=2)
        gamma = Facility.objects.create(name='Gamma', short_name='Gamma', sort_order=3)

        response = self.client.post(
            '/api/facilities/reorder/',
            {'facility_ids': [gamma.id, alpha.id, beta.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.json()], [gamma.id, alpha.id, beta.id])
        self.assertEqual(list(Facility.objects.values_list('id', flat=True)), [gamma.id, alpha.id, beta.id])

    def test_reorder_requires_every_facility_exactly_once(self):
        alpha = Facility.objects.create(name='Alpha', short_name='Alpha', sort_order=1)
        Facility.objects.create(name='Beta', short_name='Beta', sort_order=2)

        response = self.client.post(
            '/api/facilities/reorder/',
            {'facility_ids': [alpha.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 400)

    def test_unused_facility_can_be_deleted(self):
        facility = Facility.objects.create(name='Temporary', short_name='Temp')

        response = self.client.delete(f'/api/facilities/{facility.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Facility.objects.filter(id=facility.id).exists())
