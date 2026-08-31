from django.test import SimpleTestCase
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from .optimizer import _bounded_pairwise_candidates, _project_workload_change, _repair_workload_transfers, _repair_night_spacing_swaps
from .optimizer import _run_productive_repair_rounds, _round_robin_candidates, _repair_recovery_day_swaps


class BoundedPairwiseCandidateTests(SimpleTestCase):
    def test_order_and_cap_match_original_cartesian_list(self):
        left = [(1, 10), (2, 20), (3, 30)]
        right = [(4, 40), (5, 50)]
        original = [(*a, *b) for a in left for b in right]
        for limit in (0, 1, 4, 6, 100):
            self.assertEqual(list(_bounded_pairwise_candidates(left, right, limit)), original[:limit])

    def test_large_neighborhood_is_not_materialized(self):
        class LargePairs:
            def __iter__(self):
                for index in range(100000):
                    if index > 5:
                        raise AssertionError('Traversal exceeded the requested candidate budget')
                    yield index, index + 1

        # Ten billion possible combinations; only the first five are consumed.
        result = list(_bounded_pairwise_candidates(LargePairs(), LargePairs(), 5))
        self.assertEqual(result, [(0, 1, index, index + 1) for index in range(5)])


class WorkloadRankingTests(SimpleTestCase):
    def rule(self, minimum=100, maximum=140, units='HOURS'):
        return dict(min_value=Decimal(minimum), max_value=Decimal(maximum),
                    min_penalty_weight=Decimal(10000), max_penalty_weight=Decimal(10000),
                    units=units, period_type='MONTH')

    def test_month_and_block_both_count_but_other_month_does_not(self):
        periods = [
            (date(2026, 12, 1), date(2026, 12, 31), Decimal(140), self.rule()),
            (date(2027, 1, 1), date(2027, 1, 31), Decimal(149), self.rule()),
            (date(2026, 12, 1), date(2027, 1, 31), Decimal(289), self.rule(200, 280)),
        ]
        self.assertEqual(_project_workload_change(periods, date(2027, 1, 1), Decimal(9), -1), -180000)

    def test_transfer_can_overshoot_minimum_and_create_maximum_penalty(self):
        periods = [(date(2027, 1, 1), date(2027, 1, 31), Decimal(95), self.rule(100, 105))]
        self.assertEqual(_project_workload_change(periods, date(2027, 1, 1), Decimal(12), 1), -30000)

    def test_shift_units_and_unbounded_maximum(self):
        rule = self.rule(1, 2, 'SHIFTS')
        rule['max_value'] = None
        periods = [(date(2027, 1, 1), date(2027, 1, 31), Decimal(0), rule)]
        self.assertEqual(_project_workload_change(periods, date(2027, 1, 1), Decimal(12), 1), -10000)

    def test_largest_period_gain_is_checked_before_earlier_tiny_gain(self):
        windows = [(date(2026, 12, 1), date(2026, 12, 31)),
                   (date(2027, 1, 1), date(2027, 1, 31))]
        instances = [SimpleNamespace(id=1, date=windows[0][0], is_locked_open=False),
                     SimpleNamespace(id=2, date=windows[1][0], is_locked_open=False)]
        rows = []
        for pid, values in [(1, [140.0004, 140]), (2, [140, 149]), (3, [100, 86])]:
            rows.append(dict(physician_id=pid, rule_rows=[dict(
                period_start=start.isoformat(), period_end=end.isoformat(), assigned_value=value,
                deviation_direction='above_maximum' if value > 140 else 'inside_range',
            ) for (start, end), value in zip(windows, values)]))
        initial = dict(workload_score_rows=rows, score=Decimal(230004),
                       breakdown={'workload_score': Decimal(230004)})
        verified = dict(score=Decimal(50004), breakdown={'workload_score': Decimal(50004)})
        with patch('apps.scheduling.optimizer._period_windows', return_value=windows), \
             patch('apps.scheduling.optimizer._effective_workload_rule', side_effect=lambda rule, *args: rule), \
             patch('apps.scheduling.optimizer._shift_hours', return_value=Decimal(9)), \
             patch('apps.scheduling.optimizer._score_schedule', side_effect=[initial, verified]), \
             patch('apps.scheduling.optimizer._selected_physician_score_delta', return_value=-180000), \
             patch('apps.scheduling.optimizer._can_assign_in_state', return_value=True), \
             patch('apps.scheduling.optimizer._has_hard_invalids', return_value=False):
            original = {1: [1], 2: [2]}
            state, scoring, debug = _repair_workload_transfers(
                instances=instances, physicians=[], state=original, manual_pairs=set(),
                targets={pid: {'rules': [self.rule()]} for pid in (1, 2, 3)},
                contract_by_physician={}, requests_by_physician_date={},
                eligible_facilities_by_physician={}, minimum_rest_by_physician={},
                should_stop=lambda: False, candidate_limit=1,
            )
        self.assertEqual(state, {1: [1], 2: [3]})
        self.assertEqual(original, {1: [1], 2: [2]})
        self.assertEqual(debug['accepts'][0]['predicted_workload_delta'], -180000)


class NightSpacingRepairTests(SimpleTestCase):
    maximum_only = False
    recovery_only = False

    def run_repair(self, *, workload=10, night=80, total=90, invalid=False,
                   locked=False, stop=False, different_month=False, different_hours=False,
                   excess_reduced=True, receiver_night=False, locked_open=False, same_date=False):
        instances = [SimpleNamespace(id=1, date=date(2027, 1, 1), is_locked_open=False,
                                    shift_template=SimpleNamespace(night_shift=True)),
                     SimpleNamespace(id=2, date=date(2027, 2 if different_month else 1, 15), is_locked_open=locked_open,
                                     shift_template=SimpleNamespace(night_shift=receiver_night))]
        original = {1: [1], 2: [2]}
        if same_date:
            instances[1].date = instances[0].date
        baseline = {'score': 110, 'breakdown': {'night_score': 100, 'workload_score': 10}}
        candidate = {'score': total, 'breakdown': {'night_score': night, 'workload_score': workload}}
        report = {'night_violations': [{'violation_type': 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK',
                                       'shift_instance_ids': [1], 'physician_id': 1, 'penalty': 20}]}
        if self.maximum_only:
            report['night_violations'][0].update(violation_type='NIGHT_OVER_MAXIMUM', actual_value=8, configured_limit=7)
        if self.recovery_only:
            instances[0].shift_template.night_shift = False
            report['night_violations'][0].update(
                violation_type='INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT',
                next_assignment={'shift_instance_id': 1})
        def night_report(instances, physicians, state, contracts):
            if self.maximum_only and state != original and excess_reduced:
                return {'night_violations': []}
            return report
        with patch('apps.scheduling.optimizer._score_schedule', return_value=baseline), \
             patch('apps.scheduling.optimizer._night_violation_report', side_effect=night_report), \
             patch('apps.scheduling.optimizer._shift_hours', side_effect=lambda x: 9 if different_hours and x.id == 2 else 8), \
             patch('apps.scheduling.optimizer._has_hard_invalids', return_value=invalid), \
             patch('apps.scheduling.optimizer.evaluate_plateau_pairwise_swap', return_value={
                 'legal': True, 'scoring': candidate, 'state': {1: [2], 2: [1]},
             }) as evaluate:
            repair = _repair_recovery_day_swaps if self.recovery_only else _repair_night_spacing_swaps
            state, score, debug = repair(
                instances=instances, physicians=[], state=original, manual_pairs={(2, 2)} if locked else set(),
                targets={}, contract_by_physician={}, requests_by_physician_date={},
                eligible_facilities_by_physician={}, minimum_rest_by_physician={},
                should_stop=lambda: stop, candidate_limit=1,
                **({} if self.recovery_only else {'maximum_only': self.maximum_only}),
            )
        self.assertEqual(original, {1: [1], 2: [2]})
        return state, debug, evaluate.call_count

    def test_accepts_verified_night_gain_with_unchanged_workload(self):
        state, debug, calls = self.run_repair()
        self.assertEqual(state, {1: [2], 2: [1]})
        self.assertEqual(len(debug['accepts']), 1)
        self.assertEqual(debug['attempts'], 1)

    def test_rejects_workload_regression_hard_invalidity_or_non_improvement(self):
        for options in ({'workload': 11}, {'invalid': True}, {'night': 100}, {'total': 110}):
            with self.subTest(options=options):
                state, debug, calls = self.run_repair(**options)
                self.assertEqual(state, {1: [1], 2: [2]})
                self.assertEqual(debug['accepts'], [])


    def test_locks_deadline_month_and_hours_restrict_candidates(self):
        for options in ({'locked': True}, {'stop': True}, {'different_month': True}, {'different_hours': True}):
            with self.subTest(options=options):
                state, debug, calls = self.run_repair(**options)
                self.assertEqual(calls, 0)
                self.assertEqual(debug['accepts'], [])


class NightMaximumRepairTests(NightSpacingRepairTests):
    maximum_only = True

    def test_rejects_merely_moving_excess_to_another_physician(self):
        state, debug, calls = self.run_repair(excess_reduced=False)
        self.assertEqual(state, {1: [1], 2: [2]})
        self.assertEqual(debug['accepts'], [])

    def test_only_non_night_unlocked_counterparts(self):
        for options in ({'receiver_night': True}, {'locked_open': True}):
            state, debug, calls = self.run_repair(**options)
            self.assertEqual(calls, 0)


class RecoveryDayRepairTests(NightSpacingRepairTests):
    recovery_only = True

    def test_non_night_counterparts_and_locked_open(self):
        for options in ({'receiver_night': True}, {'locked_open': True}, {'same_date': True}):
            state, debug, calls = self.run_repair(**options)
            self.assertEqual(calls, 0)

    def test_round_robin_is_lazy_and_interleaves_unequal_streams(self):
        self.assertEqual(list(_round_robin_candidates([[1, 2, 3], [], [4, 5]])), [1, 4, 2, 5, 3])
        def endless():
            while True:
                yield 1
        stream = _round_robin_candidates([endless(), [2]])
        self.assertEqual([next(stream), next(stream)], [1, 2])

    def test_candidate_budget_reaches_each_physician_before_revisiting(self):
        instances = [SimpleNamespace(id=sid, date=date(2027, 1, day), is_locked_open=False,
                                     shift_template=SimpleNamespace(night_shift=False))
                     for sid, day in [(1, 1), (2, 10), (3, 20), (4, 2), (5, 11), (6, 21)]]
        state = {1: [1], 2: [2], 3: [3], 4: [99], 5: [99], 6: [99]}
        report = {'night_violations': [dict(
            violation_type='INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT',
            physician_id=pid, next_assignment={'shift_instance_id': pid}) for pid in (1, 2, 3)]}
        with patch('apps.scheduling.optimizer._score_schedule', return_value={'score': 100}), \
             patch('apps.scheduling.optimizer._night_violation_report', return_value=report), \
             patch('apps.scheduling.optimizer._shift_hours', return_value=8), \
             patch('apps.scheduling.optimizer.evaluate_plateau_pairwise_swap', return_value={'legal': False}) as evaluate:
            _repair_recovery_day_swaps(
                instances=instances, physicians=[], state=state, manual_pairs=set(), targets={},
                contract_by_physician={}, requests_by_physician_date={}, eligible_facilities_by_physician={},
                minimum_rest_by_physician={}, should_stop=lambda: False, candidate_limit=3)
        self.assertEqual([call.kwargs['left_physician_id'] for call in evaluate.call_args_list], [1, 2, 3])


class ProductiveRepairRoundTests(SimpleTestCase):
    def exercise(self, gains, deadline=20, candidate_limit=100, max_rounds=5):
        clock = [0.0]
        calls = []
        def repair(*, state, candidate_limit, should_stop):
            index = len(calls)
            calls.append(candidate_limit)
            while not should_stop():
                clock[0] += 1
            return state, {}, {'attempts': 1, 'accepts': [{}] if gains[index] else [],
                               'stopped_reason': 'time_budget'}
        result = _run_productive_repair_rounds(
            repair, state={}, elapsed=lambda: clock[0], deadline=deadline,
            candidate_limit=candidate_limit, max_rounds=max_rounds,
        )
        return result[2], calls, clock[0]

    def test_renews_productive_round_but_stops_after_no_gain(self):
        debug, calls, elapsed = self.exercise([True, False])
        self.assertEqual(calls, [100, 99])
        self.assertEqual(len(debug['rounds']), 2)
        self.assertEqual(elapsed, 8)

    def test_deadline_reserve_is_not_extended_by_progress(self):
        debug, calls, elapsed = self.exercise([True, True], deadline=6)
        self.assertEqual(elapsed, 6)
        self.assertEqual(len(calls), 2)

    def test_candidate_budget_is_shared_between_rounds(self):
        debug, calls, elapsed = self.exercise([True], candidate_limit=1)
        self.assertEqual(calls, [1])
        self.assertEqual(debug['stopped_reason'], 'candidate_budget')

    def test_round_limit_and_expired_deadline(self):
        debug, calls, elapsed = self.exercise([True], max_rounds=1)
        self.assertEqual(debug['stopped_reason'], 'round_budget')
        debug, calls, elapsed = self.exercise([], deadline=0)
        self.assertEqual(calls, [])
