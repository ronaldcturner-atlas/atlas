import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.scheduling.models import (
    Contract,
    ContractUserAssignment,
    Domain,
    OptimizerControl,
    OptimizerRun,
    ScheduleBlock,
    ScheduleVersion,
)
from .models import Physician


class AccountsTests(TestCase):
    def test_physician_email_is_required_username_and_phone_is_optional(self):
        manager = get_user_model().objects.create_user(
            username='manager@example.com',
            email='manager@example.com',
            password='atlas',
        )
        self.client.force_login(manager)

        missing_email = self.client.post(
            '/api/physicians/',
            data=json.dumps({'first_name': 'Ava', 'last_name': 'Patel'}),
            content_type='application/json',
        )
        self.assertEqual(missing_email.status_code, 400)
        self.assertIn('email', missing_email.json())

        created = self.client.post(
            '/api/physicians/',
            data=json.dumps({
                'first_name': 'Ava',
                'last_name': 'Patel',
                'email': '  AVA.PATEL@EXAMPLE.COM ',
                'phone_number': ' (843) 555-0123 ',
            }),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 201)
        physician = Physician.objects.select_related('user').get(
            id=created.json()['id'],
        )
        self.assertEqual(physician.user.email, 'ava.patel@example.com')
        self.assertEqual(physician.user.username, 'ava.patel@example.com')
        self.assertEqual(physician.phone_number, '(843) 555-0123')

        updated = self.client.patch(
            f'/api/physicians/{physician.id}/',
            data=json.dumps({
                'email': 'ava.new@example.com',
                'phone_number': '',
            }),
            content_type='application/json',
        )
        self.assertEqual(updated.status_code, 200)
        physician.refresh_from_db()
        physician.user.refresh_from_db()
        self.assertEqual(physician.user.username, 'ava.new@example.com')
        self.assertEqual(physician.phone_number, '')

    def test_login_accepts_email_for_legacy_username(self):
        get_user_model().objects.create_user(
            username='legacy-name',
            email='legacy@example.com',
            password='atlas',
        )

        response = self.client.post(
            '/api/login/',
            data=json.dumps({
                'username': 'LEGACY@EXAMPLE.COM',
                'password': 'atlas',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['email'], 'legacy@example.com')

    def test_physician_profile_includes_current_contract_and_roles(self):
        manager = get_user_model().objects.create_user(
            username='manager@example.com',
            email='manager@example.com',
            password='atlas',
        )
        user = get_user_model().objects.create_user(
            username='ava@example.com',
            email='ava@example.com',
            first_name='Ava',
            last_name='Patel',
        )
        user.groups.add(Group.objects.create(name='Scheduler'))
        physician = Physician.objects.create(user=user, role='scheduler')
        domain = Domain.objects.create(name='Physician')
        contract = Contract.objects.create(domain=domain, name='Full Time')
        ContractUserAssignment.objects.create(
            contract=contract,
            physician=physician,
            domain=domain,
        )
        self.client.force_login(manager)

        response = self.client.get(f'/api/physicians/{physician.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['role'], 'scheduler')
        self.assertEqual(response.json()['current_contracts'], [{
            'id': contract.id,
            'name': 'Full Time',
            'domain_id': domain.id,
            'domain': 'Physician',
        }])

    def test_create_demo_user_assigns_scheduler_role_to_existing_user(self):
        user = get_user_model().objects.create_user(username='ron', password='atlas')

        call_command('create_demo_user', verbosity=0)
        call_command('create_demo_user', verbosity=0)

        user.refresh_from_db()
        self.assertTrue(user.groups.filter(name='Scheduler').exists())

    def test_logout_does_not_stop_or_remove_background_optimizer(self):
        user = get_user_model().objects.create_user(
            username='logout-scheduler', password='atlas',
        )
        domain = Domain.objects.create(name='Logout Test Domain')
        block = ScheduleBlock.objects.create(
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
            request_open_datetime=timezone.now(),
            request_close_datetime=timezone.now(),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=domain,
            version_number=1,
            status=ScheduleVersion.Status.BUILD,
        )
        run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            created_by=user,
            status=OptimizerRun.Status.RUNNING,
        )
        control = OptimizerControl.objects.create(
            token='19764223-ded6-4fcb-b962-b92f68962062',
            schedule_version=version,
            optimizer_run=run,
            created_by=user,
            started_at=timezone.now(),
        )
        self.client.force_login(user)

        response = self.client.post('/api/logout/')

        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        control.refresh_from_db()
        self.assertEqual(run.status, OptimizerRun.Status.RUNNING)
        self.assertFalse(control.stop_requested)
