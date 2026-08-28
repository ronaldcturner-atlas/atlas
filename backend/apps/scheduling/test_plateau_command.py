import json
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
    Contract, ContractUserAssignment, OptimizerRun, ScheduleBlock,
    ScheduleShiftAssignment, ScheduleShiftInstance, ScheduleVersion, ShiftTemplate,
)


class ExplainOptimizerPlateauCommandTests(TestCase):
    def test_command_reports_rejection_reason_without_mutating_source_run(self):
        domain = Domain.objects.create(name='Physician', active=True)
        facility = Facility.objects.create(name='Test Hospital', short_name='Test')
        block = ScheduleBlock.objects.create(
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1),
            request_open_datetime=timezone.make_aware(datetime(2026, 6, 1, 8)),
            request_close_datetime=timezone.make_aware(datetime(2026, 6, 2, 8)),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=domain,
            version_number=1,
            name='Plateau fixture',
            status=ScheduleVersion.Status.BUILD,
        )
        template = ShiftTemplate.objects.create(
            facility=facility,
            start_time=time(7),
            end_time=time(16),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            default_staffing_count=1,
        )
        instance = ScheduleShiftInstance.objects.create(
            schedule_block=block,
            schedule_version=version,
            shift_template=template,
            facility=facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 1, 16)),
            required_staffing=1,
        )
        physicians = []
        for index in range(2):
            user = get_user_model().objects.create_user(
                username=f'plateau{index}@example.com',
                first_name='Plateau',
                last_name=str(index),
            )
            physician = Physician.objects.create(user=user, display_name=f'Plateau {index}')
            contract = Contract.objects.create(
                domain=domain,
                name=f'Plateau Contract {index}',
                workload_settings={
                    'period_rules': [{
                        'period_type': 'SCHEDULE_BLOCK',
                        'units': 'HOURS',
                        'min_value': '10',
                        'max_value': '20',
                        'min_penalty_weight': '100',
                        'max_penalty_weight': '100',
                    }],
                },
            )
            contract.facilities.add(facility)
            ContractUserAssignment.objects.create(
                contract=contract, domain=domain, physician=physician,
            )
            physicians.append(physician)
        source = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            initial_score=100,
            final_score=100,
            optimizer_summary={'marker': 'unchanged'},
            run_kind='BENCHMARK',
            is_active=True,
        )
        assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=instance,
            physician=physicians[0],
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run=source,
            is_locked=True,
        )
        source_snapshot = {
            'initial_score': source.initial_score,
            'final_score': source.final_score,
            'optimizer_summary': source.optimizer_summary,
            'is_active': source.is_active,
            'assignment': (
                assignment.shift_instance_id, assignment.physician_id,
                assignment.assignment_source, assignment.is_locked,
            ),
        }
        stdout = StringIO()

        call_command(
            'explain_optimizer_plateau',
            schedule_block_id=block.id,
            domain='Physician',
            optimizer_run_id=source.id,
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertGreater(payload['total_score'], 0)
        self.assertTrue(payload['violations'])
        rejection_reasons = {
            reason
            for violation in payload['violations']
            for attempt in violation.get('candidate_moves', [])
            for reason in attempt['rejection_reasons']
        }
        self.assertIn('locked_assignment', rejection_reasons)
        workload_violation = next(
            violation for violation in payload['violations']
            if violation['category'] == 'workload'
        )
        self.assertIn('candidate_swaps', workload_violation)
        self.assertGreater(workload_violation['candidate_moves_attempted'], 0)
        self.assertGreater(workload_violation['candidate_swaps_attempted'], 0)
        self.assertIn('best_workload_score_delta_found', workload_violation)
        self.assertIn('best_total_score_delta_found', workload_violation)
        self.assertTrue(workload_violation['top_rejected_reasons'])
        self.assertIn('nonzero_component_scores', payload)
        self.assertIn('best_single_move', payload['summary'])
        self.assertIn('best_pairwise_swap', payload['summary'])
        self.assertIn('local_optimum_under_single_moves', payload['summary'])
        self.assertIn('local_optimum_under_pairwise_swaps', payload['summary'])
        self.assertIn('local optimum', payload['summary']['conclusion'])
        source.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(
            {
                'initial_score': source.initial_score,
                'final_score': source.final_score,
                'optimizer_summary': source.optimizer_summary,
                'is_active': source.is_active,
                'assignment': (
                    assignment.shift_instance_id, assignment.physician_id,
                    assignment.assignment_source, assignment.is_locked,
                ),
            },
            source_snapshot,
        )
