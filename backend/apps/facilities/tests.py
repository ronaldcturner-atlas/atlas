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
