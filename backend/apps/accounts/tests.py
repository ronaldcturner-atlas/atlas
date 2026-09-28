from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.scheduling.models import (
    Domain,
    OptimizerControl,
    OptimizerRun,
    ScheduleBlock,
    ScheduleVersion,
)


class AccountsTests(TestCase):
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
