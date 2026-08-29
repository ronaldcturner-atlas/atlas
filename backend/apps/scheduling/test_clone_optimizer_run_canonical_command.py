from datetime import date, datetime, time
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Physician
from apps.domains.models import Domain
from apps.facilities.models import Facility

from .models import (
    Contract,
    ContractUserAssignment,
    OptimizerRun,
    ScheduleBlock,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    ShiftTemplate,
)
from .optimizer import build_violation_report


class CloneOptimizerRunCanonicalCommandTests(TestCase):
    def setUp(self):
        self.domain = Domain.objects.create(name='Physician', active=True)
        self.facility = Facility.objects.create(
            name='Canonical Hospital', short_name='Canonical',
        )
        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            request_open_datetime=timezone.make_aware(datetime(2026, 8, 1, 8)),
            request_close_datetime=timezone.make_aware(datetime(2026, 8, 2, 8)),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        self.version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Canonical fixture',
            status=ScheduleVersion.Status.BUILD,
        )
        template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7),
            end_time=time(17),
            active_days_of_week=['Tuesday'],
            weekend_days=[],
            default_staffing_count=2,
        )
        self.instance = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=self.version,
            shift_template=template,
            facility=self.facility,
            date=date(2026, 9, 1),
            start_datetime=timezone.make_aware(datetime(2026, 9, 1, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 1, 17)),
            required_staffing=2,
        )
        self.physicians = []
        for index in range(3):
            user = get_user_model().objects.create_user(
                username=f'canonical{index}@example.com',
                first_name='Canonical',
                last_name=str(index),
            )
            physician = Physician.objects.create(
                user=user, display_name=f'Canonical {index}',
            )
            contract = Contract.objects.create(
                domain=self.domain, name=f'Canonical Contract {index}', active=True,
            )
            contract.facilities.add(self.facility)
            ContractUserAssignment.objects.create(
                contract=contract, domain=self.domain, physician=physician,
            )
            self.physicians.append(physician)

        self.source = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            initial_score=999,
            final_score=999,
            score_breakdown={'historical': 999},
            optimizer_summary={'historical': True},
            notes='Historical source',
            is_active=True,
            run_kind='OPTIMIZER',
        )
        # Same run/physician/shift represented once as optimizer output and once
        # as a locked manual row. Canonicalization keeps the locked manual row.
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[0],
            optimizer_run=self.source,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )
        self.locked_manual = ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[0],
            optimizer_run=self.source,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            is_locked=True,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[1],
            optimizer_run=self.source,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )
        # This legacy manual row is visible beside an ordinary run, but does not
        # belong to the selected run and must not enter the clone.
        self.visible_only = ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[2],
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
        )
        self.other_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=2,
            status=OptimizerRun.Status.COMPLETED,
            final_score=0,
            run_kind='BENCHMARK',
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[2],
            optimizer_run=self.other_run,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )

    def test_clone_is_canonical_scored_and_does_not_mutate_source(self):
        source_values = {
            'final_score': self.source.final_score,
            'score_breakdown': self.source.score_breakdown,
            'optimizer_summary': self.source.optimizer_summary,
            'notes': self.source.notes,
            'is_active': self.source.is_active,
        }
        source_assignment_ids = list(
            self.source.assignments.order_by('id').values_list('id', flat=True)
        )
        stdout = StringIO()

        call_command(
            'clone_optimizer_run_canonical',
            schedule_block_id=self.block.id,
            domain=self.domain.name,
            source_run_id=self.source.id,
            stdout=stdout,
        )

        clone = OptimizerRun.objects.exclude(
            id__in=[self.source.id, self.other_run.id],
        ).get()
        clone_rows = list(clone.assignments.order_by('physician_id'))
        self.assertEqual(clone.run_kind, 'COPY')
        self.assertFalse(clone.is_active)
        self.assertEqual(len(clone_rows), 2)
        self.assertEqual(
            {row.physician_id for row in clone_rows},
            {self.physicians[0].id, self.physicians[1].id},
        )
        kept_manual = next(
            row for row in clone_rows if row.physician_id == self.physicians[0].id
        )
        self.assertEqual(
            kept_manual.assignment_source,
            ScheduleShiftAssignment.AssignmentSource.MANUAL,
        )
        self.assertTrue(kept_manual.is_locked)
        self.assertFalse(clone.assignments.filter(physician=self.physicians[2]).exists())

        report = build_violation_report(self.version, optimizer_run=clone)
        self.assertEqual(float(clone.final_score), report['total_score'])
        self.assertEqual(clone.score_breakdown, report['score_breakdown'])
        self.assertEqual(clone.initial_score, clone.final_score)

        self.source.refresh_from_db()
        self.assertEqual(
            {
                'final_score': self.source.final_score,
                'score_breakdown': self.source.score_breakdown,
                'optimizer_summary': self.source.optimizer_summary,
                'notes': self.source.notes,
                'is_active': self.source.is_active,
            },
            source_values,
        )
        self.assertEqual(
            list(self.source.assignments.order_by('id').values_list('id', flat=True)),
            source_assignment_ids,
        )
        output = stdout.getvalue()
        self.assertIn(f'source_run_id: {self.source.id}', output)
        self.assertIn(f'new_run_id: {clone.id}', output)
        self.assertIn('assignment_count_before: 4', output)
        self.assertIn('source_owned_assignment_count: 3', output)
        self.assertIn('assignment_count_after: 2', output)
        self.assertIn('duplicate_rows_excluded: 1', output)
        self.assertIn('visible_only_rows_excluded: 1', output)
