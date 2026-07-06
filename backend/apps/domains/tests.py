from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Domain


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
