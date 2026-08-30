from decimal import Decimal

from django.test import TestCase

from .management.commands.benchmark_optimizer_scale import Command, SCALE_PROFILES
from .models import Contract, ContractUserAssignment, OptimizerRun, ScheduleShiftAssignment
from .optimizer import (
    _result_priority, _should_preserve_timeout_result, optimize_schedule_version,
    build_violation_report,
)
from .run_state import assignments_for_viewed_run


class CoveragePriorityTests(TestCase):
    def test_workload_revisit_rebalances_multiple_shifts_and_preserves_lock(self):
        version = Command()._build_fixture(SCALE_PROFILES[0], shifts_per_day=1)
        contract = Contract.objects.get(domain=version.domain)
        physician = ContractUserAssignment.objects.filter(contract=contract).first().physician
        source = OptimizerRun.objects.create(
            schedule_version=version, run_number=1, status=OptimizerRun.Status.COMPLETED,
            is_active=True, initial_score=0, final_score=0,
        )
        instances = list(version.shift_instances.order_by('date'))
        for index, instance in enumerate(instances):
            ScheduleShiftAssignment.objects.create(
                shift_instance=instance, physician=physician, optimizer_run=source,
                assignment_source=(ScheduleShiftAssignment.AssignmentSource.MANUAL if index == 0
                                   else ScheduleShiftAssignment.AssignmentSource.OPTIMIZER),
                is_locked=index == 0,
            )
        summary = optimize_schedule_version(
            version, source_run=source, start_mode=OptimizerRun.StartMode.CURRENT_SCHEDULE,
            seed=551,
        )
        accepted = summary['debug']['workload_revisit']['accepts']
        self.assertGreater(len(accepted), 1)
        self.assertTrue(all(row['score_after'] < row['score_before'] for row in accepted))
        self.assertEqual(summary['unfilled_shift_count'], 0)
        self.assertLess(summary['final_score'], summary['initial_score'])
        result = OptimizerRun.objects.get(pk=summary['optimizer_run_id'])
        self.assertTrue(assignments_for_viewed_run(version, result).filter(
            shift_instance=instances[0], physician=physician, is_locked=True,
        ).exists())
        self.assertEqual(assignments_for_viewed_run(version, source).count(), 31)

    def test_complete_valid_coverage_beats_lower_penalty_incomplete_result(self):
        complete = {'validation': {}, 'score': Decimal('20000')}
        incomplete = {'validation': {}, 'score': Decimal('1000')}
        self.assertLess(_result_priority(complete, 0), _result_priority(incomplete, 1))
        self.assertGreater(_result_priority(complete, 0), _result_priority(incomplete, 0))
        invalid = {'validation': {'final_rest_violations': 1}, 'score': Decimal('0')}
        self.assertGreater(_result_priority(invalid, 0), _result_priority(complete, 0))
        self.assertTrue(_should_preserve_timeout_result(
            timed_out=True, initial_score=1000, final_score=20000,
            final_scoring=complete, unfilled_shift_count=0,
            coverage_improved_to_complete=True,
        ))
        self.assertFalse(_should_preserve_timeout_result(
            timed_out=True, initial_score=1000, final_score=20000,
            final_scoring=invalid, unfilled_shift_count=0,
            coverage_improved_to_complete=True,
        ))

    def test_current_schedule_keeps_new_coverage_despite_higher_workload_penalties(self):
        version = Command()._build_fixture(SCALE_PROFILES[0], shifts_per_day=1)
        contract = Contract.objects.get(domain=version.domain)
        settings = {'period_rules': [{
            'period_type': 'SCHEDULE_BLOCK', 'units': 'SHIFTS',
            'min_value': '0', 'max_value': '1', 'max_penalty_weight': '100000',
        }]}
        contract.workload_settings = settings
        contract.save(update_fields=['workload_settings'])
        source = OptimizerRun.objects.create(
            schedule_version=version, run_number=1, status=OptimizerRun.Status.COMPLETED,
            is_active=True, initial_score=0, final_score=0,
        )
        first = version.shift_instances.order_by('date').first()
        physician = ContractUserAssignment.objects.filter(contract=contract).first().physician
        ScheduleShiftAssignment.objects.create(
            shift_instance=first, physician=physician, optimizer_run=source,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL, is_locked=True,
        )
        summary = optimize_schedule_version(
            version, source_run=source, start_mode=OptimizerRun.StartMode.CURRENT_SCHEDULE,
            seed=441,
        )
        result = OptimizerRun.objects.get(pk=summary['optimizer_run_id'])
        self.assertEqual(result.status, OptimizerRun.Status.COMPLETED)
        self.assertEqual(summary['unfilled_shift_count'], 0)
        self.assertEqual(assignments_for_viewed_run(version, result).count(), 31)
        self.assertEqual(summary['assignments_made'], 30)
        self.assertGreater(summary['final_score'], summary['initial_score'])
        self.assertTrue(summary['debug']['coverage_improved_to_complete'])
        self.assertFalse(summary['debug']['source_state_restored_as_best'])
        self.assertTrue(assignments_for_viewed_run(version, result).filter(
            shift_instance=first, physician=physician, is_locked=True,
        ).exists())
        contract.refresh_from_db()
        self.assertEqual(contract.workload_settings, settings)
        self.assertEqual(build_violation_report(version, result)['total_score'], summary['final_score'])
        self.assertEqual(assignments_for_viewed_run(version, source).count(), 1)
