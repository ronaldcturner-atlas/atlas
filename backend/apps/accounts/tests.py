from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase


class AccountsTests(TestCase):
    def test_create_demo_user_assigns_scheduler_role_to_existing_user(self):
        user = get_user_model().objects.create_user(username='ron', password='atlas')

        call_command('create_demo_user', verbosity=0)
        call_command('create_demo_user', verbosity=0)

        user.refresh_from_db()
        self.assertTrue(user.groups.filter(name='Scheduler').exists())
