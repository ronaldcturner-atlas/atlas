from django.contrib.auth.models import User
from django.test import TestCase
from .models import OptimizerRun, ScheduleBlock, ScheduleShiftAssignment
from .management.commands.benchmark_optimizer_generalization import CASES, run_case, ScreeningRunner
from .management.commands.benchmark_optimizer_scale import SCALE_PROFILES
from .optimizer import optimize_schedule_version, build_violation_report
from .run_state import assignments_for_viewed_run
from unittest.mock import patch
from collections import Counter, defaultdict
from datetime import timedelta


class GeneralizationBenchmarkTests(TestCase):
    def test_stopped_runs_preserve_visible_locks_and_match_saved_score(self):
        for start in ('fresh', 'partial', 'balanced'):
            for kind in ('OPTIMIZER', 'BENCHMARK'):
                with self.subTest(start=start, kind=kind):
                    runner = ScreeningRunner(starting_point=start, scenario='mixed')
                    profile = {**SCALE_PROFILES[0], 'name': f'1m-5p-{start}-{kind}'}
                    version = runner._build_fixture(profile, shifts_per_day=2,
                        staffing_per_shift=1, scenario='mixed')
                    originals = set(ScheduleShiftAssignment.objects.filter(
                        shift_instance__schedule_version=version, is_locked=True
                    ).values_list('id', 'shift_instance_id', 'physician_id'))
                    source_pairs = set(assignments_for_viewed_run(version, runner.source)
                        .values_list('shift_instance_id', 'physician_id'))
                    with patch('apps.scheduling.optimizer.MAX_RUNTIME_SECONDS', 0):
                        summary = optimize_schedule_version(version, source_run=runner.source,
                            start_mode='FRESH_FILL' if start == 'fresh' else 'CURRENT_SCHEDULE',
                            run_kind=kind, seed=71, adaptive_runtime=True, stop_requested=lambda: True)
                    result = OptimizerRun.objects.get(pk=summary['optimizer_run_id'])
                    visible = assignments_for_viewed_run(version, result)
                    # Validate saved rows directly, not optimizer diagnostics.
                    rows = list(visible.select_related('shift_instance', 'physician'))
                    pairs = [(row.shift_instance_id, row.physician_id) for row in rows]
                    self.assertEqual(len(pairs), len(set(pairs)))
                    counts = Counter(sid for sid, _ in pairs)
                    intervals = defaultdict(list)
                    for row in rows:
                        self.assertTrue(row.physician.active)
                        self.assertIn(row.shift_instance.facility_id,
                            version.domain.contracts.get().facilities.values_list('id', flat=True))
                        intervals[row.physician_id].append((row.shift_instance.start_datetime,
                                                           row.shift_instance.end_datetime))
                    for periods in intervals.values():
                        periods.sort()
                        for previous, following in zip(periods, periods[1:]):
                            # These synthetic fixtures use the default ten-hour rest rule.
                            self.assertGreaterEqual(following[0] - previous[1], timedelta(hours=10))
                    for instance in version.shift_instances.all():
                        self.assertLessEqual(counts[instance.id], instance.required_staffing)
                        self.assertEqual(instance.status, 'ASSIGNED' if counts[instance.id] ==
                                         instance.required_staffing else 'OPEN')
                    self.assertTrue({(sid, pid) for _, sid, pid in originals} <= set(
                        visible.filter(is_locked=True).values_list('shift_instance_id', 'physician_id')))
                    self.assertEqual(originals, set(ScheduleShiftAssignment.objects.filter(
                        id__in=[pk for pk, _, _ in originals]
                    ).values_list('id', 'shift_instance_id', 'physician_id')))
                    self.assertAlmostEqual(float(result.final_score),
                        float(build_violation_report(version, result)['total_score']), places=2)
                    self.assertEqual(summary['debug']['adaptive_runtime']['stopped_reason'], 'user_stop')
                    if runner.source:
                        self.assertEqual(source_pairs, set(assignments_for_viewed_run(version, runner.source)
                            .values_list('shift_instance_id', 'physician_id')))
                    if start == 'balanced':
                        self.assertEqual(result.status, OptimizerRun.Status.COMPLETED)
                        self.assertEqual(summary['unfilled_shift_count'], 0)
                    else:
                        self.assertEqual(result.status, OptimizerRun.Status.FAILED)
                        self.assertFalse(result.is_active)

    def test_matrix_covers_sizes_starts_and_incompatible_soft_limits(self):
        self.assertEqual({c[0] for c in CASES}, {p['name'] for p in SCALE_PROFILES})
        self.assertEqual({c[1] for c in CASES}, {'fresh', 'partial', 'uneven', 'balanced'})
        self.assertIn('conflicting', {c[2] for c in CASES})

    def test_paired_cases_have_same_start_and_roll_back_everything(self):
        models = [User, ScheduleBlock, OptimizerRun, ScheduleShiftAssignment]
        before = [model.objects.count() for model in models]
        results = [run_case(('1m-5p', 'partial', 'mixed'), seed=1, variant=variant, seconds=.1)
                   for variant in ('current', 'without_targeted_repairs', 'without_general_repair')]
        self.assertEqual(before, [model.objects.count() for model in models])
        self.assertEqual(results[0]['initial_score'], results[1]['initial_score'])
        self.assertEqual(results[0]['starting_slots'], results[1]['starting_slots'])
        for result in results:
            self.assertGreater(result['starting_slots'], 0)
            self.assertLess(result['starting_slots'], result['required_slots'])
            self.assertTrue(result['quality_gates']['contracts_unchanged'])
            self.assertTrue(result['quality_gates']['fixed_assignments_preserved'])

    def test_uneven_start_is_complete_and_period_rules_coexist(self):
        runner = ScreeningRunner(starting_point='uneven', scenario='mixed')
        version = runner._build_fixture(SCALE_PROFILES[0], shifts_per_day=2, staffing_per_shift=1, scenario='mixed')
        self.assertEqual(runner.starting_slots, 62)
        contract = version.domain.contracts.get()
        self.assertEqual({r['period_type'] for r in contract.workload_settings['period_rules']},
                         {'WEEK', 'MONTH', 'SCHEDULE_BLOCK'})
