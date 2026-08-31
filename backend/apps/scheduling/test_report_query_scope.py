from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import optimizer
from .management.commands.benchmark_optimizer_generalization import ScreeningRunner
from .management.commands.benchmark_optimizer_scale import SCALE_PROFILES
from .models import OptimizerRun, ScheduleShiftAssignment


class ReportQueryScopeTests(TestCase):
    def test_history_is_not_preloaded_and_report_is_unchanged(self):
        runner = ScreeningRunner(starting_point='partial', scenario='mixed')
        version = runner._build_fixture(SCALE_PROFILES[1], shifts_per_day=2,
            staffing_per_shift=2, scenario='mixed')
        pairs = list(runner.source.assignments.values_list('shift_instance_id', 'physician_id'))
        for number in (2, 3, 4):
            historical = OptimizerRun.objects.create(schedule_version=version,
                run_number=number, status=OptimizerRun.Status.COMPLETED)
            ScheduleShiftAssignment.objects.bulk_create([
                ScheduleShiftAssignment(shift_instance_id=sid, physician_id=pid,
                    optimizer_run=historical, assignment_source='OPTIMIZER') for sid, pid in pairs
            ])

        original_queryset = optimizer._version_shift_instances_queryset
        def with_old_prefetch(v):
            return original_queryset(v).prefetch_related('assignments__physician__user')

        with patch.object(optimizer, '_version_shift_instances_queryset', side_effect=with_old_prefetch):
            with CaptureQueriesContext(connection) as old_queries:
                expected = optimizer.build_violation_report(version, runner.source)
        with CaptureQueriesContext(connection) as new_queries:
            actual = optimizer.build_violation_report(version, runner.source)
        self.assertEqual(actual, expected)
        self.assertLess(len(new_queries), len(old_queries))
        # Only the authoritative assignment query remains, not an unscoped
        # reverse-relation fetch spanning every optimizer run in history.
        assignment_queries = [q['sql'] for q in new_queries if
            'FROM "scheduling_scheduleshiftassignment"' in q['sql']]
        self.assertEqual(len(assignment_queries), 1)
        self.assertIn('optimizer_run_id', assignment_queries[0])
