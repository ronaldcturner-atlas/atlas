from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Physician
from apps.domains.models import Domain
from apps.facilities.models import Facility

from .models import (
    OptimizerRun,
    ScheduleBlock,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    ShiftTemplate,
)
from .run_state import (
    assignments_for_viewed_run,
    locked_open_ids,
    resolve_build_workspace_run_context,
)


class RunStateInvariantTests(TestCase):
    def setUp(self):
        self.domain = Domain.objects.create(name='Physician', active=True)
        self.facility = Facility.objects.create(name='Run State Hospital', short_name='RSH')
        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 1),
            request_open_datetime=timezone.make_aware(datetime(2026, 10, 1, 8)),
            request_close_datetime=timezone.make_aware(datetime(2026, 10, 2, 8)),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        self.version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Run-state fixture',
            status=ScheduleVersion.Status.BUILD,
        )
        template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7),
            end_time=time(16),
            active_days_of_week=['Sunday'],
            weekend_days=[],
            default_staffing_count=4,
        )
        self.instance = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=self.version,
            shift_template=template,
            facility=self.facility,
            date=date(2026, 11, 1),
            start_datetime=timezone.make_aware(datetime(2026, 11, 1, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 11, 1, 16)),
            required_staffing=4,
        )
        self.physicians = []
        for index in range(4):
            user = get_user_model().objects.create_user(
                username=f'run-state-{index}@example.com',
            )
            self.physicians.append(Physician.objects.create(user=user))

        self.optimizer_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='OPTIMIZER',
            is_active=True,
            locked_open_shift_instance_ids=[self.instance.id],
        )
        self.copy_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=2,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='COPY',
        )
        self.benchmark_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=3,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='BENCHMARK',
        )
        rows = (
            (self.physicians[0], None, ScheduleShiftAssignment.AssignmentSource.MANUAL),
            (self.physicians[1], self.optimizer_run, ScheduleShiftAssignment.AssignmentSource.OPTIMIZER),
            (self.physicians[2], self.copy_run, ScheduleShiftAssignment.AssignmentSource.OPTIMIZER),
            (self.physicians[3], self.benchmark_run, ScheduleShiftAssignment.AssignmentSource.OPTIMIZER),
        )
        for physician, run, source in rows:
            ScheduleShiftAssignment.objects.create(
                shift_instance=self.instance,
                physician=physician,
                optimizer_run=run,
                assignment_source=source,
            )

    def visible_physician_ids(self, viewed_run):
        return set(
            assignments_for_viewed_run(self.version, viewed_run)
            .values_list('physician_id', flat=True)
        )

    def test_assignment_visibility_matrix(self):
        legacy, optimizer, copy, benchmark = [physician.id for physician in self.physicians]

        self.assertEqual(self.visible_physician_ids(None), {legacy})
        self.assertEqual(
            self.visible_physician_ids(self.optimizer_run),
            {legacy, optimizer},
        )
        self.assertEqual(self.visible_physician_ids(self.copy_run), {copy})
        self.assertEqual(self.visible_physician_ids(self.benchmark_run), {benchmark})

    def test_viewed_and_active_roles_control_permissions_without_activation(self):
        context = resolve_build_workspace_run_context(
            self.version,
            requested_run_id=self.copy_run.id,
        )

        self.assertEqual(context.viewed_run, self.copy_run)
        self.assertEqual(context.active_run, self.optimizer_run)
        self.assertFalse(context.viewed_run_is_editable)
        self.assertTrue(context.viewed_run_can_activate)
        self.assertTrue(context.viewed_run_can_copy)
        self.optimizer_run.refresh_from_db()
        self.copy_run.refresh_from_db()
        self.assertTrue(self.optimizer_run.is_active)
        self.assertFalse(self.copy_run.is_active)

    def test_noncompleted_requested_run_falls_back_to_active_completed_run(self):
        running = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=4,
            status=OptimizerRun.Status.RUNNING,
            run_kind='OPTIMIZER',
        )

        context = resolve_build_workspace_run_context(
            self.version,
            requested_run_id=running.id,
        )

        self.assertEqual(context.viewed_run, self.optimizer_run)
        self.assertEqual(context.active_run, self.optimizer_run)
        self.assertTrue(context.viewed_run_is_editable)
        self.assertFalse(context.viewed_run_can_activate)

    def test_locked_open_snapshot_comes_from_viewed_run(self):
        self.assertEqual(locked_open_ids(self.optimizer_run), {self.instance.id})
        self.assertEqual(locked_open_ids(self.copy_run), set())
        self.assertEqual(locked_open_ids(None), set())
