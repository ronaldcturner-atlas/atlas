from decimal import Decimal
from datetime import date
from random import Random
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer
from .search_budget import SearchBudget


def valid_scoring(score):
    return {
        'score': Decimal(str(score)),
        'validation': {
            'final_overlap_violations': 0,
            'final_rest_violations': 0,
            'final_duplicate_violations': 0,
            'final_overstaffed_violations': 0,
            'final_inactive_physician_violations': 0,
            'final_facility_ineligible_violations': 0,
        },
    }


class FakeBudget:
    def __init__(self):
        self.best_score = Decimal('10')
        self.stopped = False
        self.restart_exhausted = False

    def reason(self):
        return 'stall_limit' if self.stopped else None

    def observe(self, score, *, valid):
        if not valid or score >= self.best_score:
            return False
        self.best_score = score
        return True

    def restart_after_stall(self):
        return False


class AdaptiveSearchRoundTests(SimpleTestCase):
    def test_pipeline_exhaustion_requires_consecutive_unproductive_epochs(self):
        exhausted_count = 0
        exhausted_count = optimizer._next_exhausted_pipeline_epoch_count(
            exhausted_count, productive=False,
        )
        exhausted_count = optimizer._next_exhausted_pipeline_epoch_count(
            exhausted_count, productive=False,
        )
        self.assertEqual(exhausted_count, 2)

        exhausted_count = optimizer._next_exhausted_pipeline_epoch_count(
            exhausted_count, productive=True,
        )
        self.assertEqual(exhausted_count, 0)

        for _ in range(optimizer.MAX_CONSECUTIVE_EXHAUSTED_PIPELINE_EPOCHS):
            exhausted_count = optimizer._next_exhausted_pipeline_epoch_count(
                exhausted_count, productive=False,
            )
        self.assertEqual(
            exhausted_count,
            optimizer.MAX_CONSECUTIVE_EXHAUSTED_PIPELINE_EPOCHS,
        )

    def test_productive_search_is_not_interrupted_by_a_fixed_epoch_limit(self):
        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        clock = Clock()
        budget = SearchBudget(
            started_at=clock(), stall_seconds=100, total_seconds=5,
            clock=clock,
        )
        budget.observe(Decimal('100'), valid=True)

        def improving_repair(**kwargs):
            clock.value += 0.6
            # Derive from the budget's retained global score so every renewed
            # epoch continues the same best chain.
            next_scoring = valid_scoring(budget.best_score - 1)
            kwargs['on_improvement'](kwargs['state'], next_scoring)
            return kwargs['state'], next_scoring, {
                'attempts': 1,
                'legal_candidates': 1,
                'scored_candidates': 1,
                'accepts': [{'score': float(next_scoring['score'])}],
                'stopped_reason': 'improved',
            }

        repair_names = (
            '_repair_weekend_support_swaps',
            '_repair_general_constraint_reassignments',
            '_repair_general_constraint_swaps',
            '_repair_workload_transfers',
            '_repair_night_minimum_distribution',
            '_repair_night_spacing_swaps',
            '_repair_recovery_day_swaps',
        )
        patches = [patch.object(optimizer, name, side_effect=improving_repair) for name in repair_names]
        progress_scores = []
        for repair_patch in patches:
            repair_patch.start()
        try:
            _state, scoring, debug = optimizer._run_adaptive_search_rounds(
                instances=[SimpleNamespace(
                    id=1, required_staffing=1, is_locked_open=False,
                )],
                physicians=[SimpleNamespace(id=1)],
                initial_state={1: [1]}, initial_scoring=valid_scoring(100),
                manual_pairs=set(), targets={}, contract_by_physician={},
                requests_by_physician_date={}, eligible_facilities_by_physician={},
                minimum_rest_by_physician={}, search_budget=budget, rng=Random(10),
                debug={'cycles': 0, 'improvements': 0, 'attempts': 0},
                progress_callback=progress_scores.append,
            )
        finally:
            for repair_patch in reversed(patches):
                repair_patch.stop()

        self.assertLess(scoring['score'], Decimal('100'))
        self.assertEqual(debug['pipeline_epoch_restarts'], 0)
        self.assertEqual(len(debug['pipeline_epochs']), 1)
        self.assertEqual(debug['pipeline_epoch_controller'], 'productivity_driven')
        self.assertIsNone(debug['pipeline_epoch_seconds'])
        self.assertTrue(debug['pipeline_epochs'][0]['productive'])
        self.assertTrue(progress_scores)
        self.assertEqual(progress_scores[-1], scoring['score'])

    def test_complete_state_outranks_cheaper_incomplete_state(self):
        instances = [SimpleNamespace(id=1, required_staffing=1)]

        self.assertLess(
            optimizer._state_result_priority(
                instances, {1: [1]}, valid_scoring(100),
            ),
            optimizer._state_result_priority(
                instances, {1: []}, valid_scoring(1),
            ),
        )

    def test_direct_coverage_repair_can_promote_complete_higher_score_state(self):
        budget = FakeBudget()
        budget.best_score = Decimal('1')
        calls = []

        def direct_coverage(**kwargs):
            calls.append('direct_coverage')
            completed = {1: [1]}
            completed_scoring = valid_scoring(100)
            kwargs['on_improvement'](completed, completed_scoring)
            budget.stopped = True
            return completed, completed_scoring, {
                'attempts': 1,
                'legal_candidates': 1,
                'scored_candidates': 1,
                'accepts': [{'shift_instance_id': 1}],
                'stopped_reason': 'coverage_complete',
            }

        with patch.object(
            optimizer, '_repair_direct_coverage', side_effect=direct_coverage,
        ):
            state, scoring, debug = optimizer._run_adaptive_search_rounds(
                instances=[SimpleNamespace(
                    id=1, required_staffing=1, is_locked_open=False,
                )],
                physicians=[SimpleNamespace(id=1)],
                initial_state={1: []}, initial_scoring=valid_scoring(1),
                manual_pairs=set(), targets={}, contract_by_physician={},
                requests_by_physician_date={}, eligible_facilities_by_physician={},
                minimum_rest_by_physician={}, search_budget=budget, rng=Random(9),
                debug={'cycles': 0, 'improvements': 0, 'attempts': 0},
            )

        self.assertEqual(calls, ['direct_coverage'])
        self.assertEqual(state, {1: [1]})
        self.assertEqual(scoring['score'], Decimal('100'))
        self.assertEqual(debug['improvements'], 1)

    def test_repair_order_prioritizes_roi_and_skips_cooling_strategies(self):
        repairs = [
            ('cold', object(), {}),
            ('productive', object(), {}),
            ('unproductive', object(), {}),
            ('cooling', object(), {}),
        ]
        stats = {
            'productive': {
                'calls': 3,
                'score_improvement': 120,
                'runtime_seconds': 4,
            },
            'unproductive': {
                'calls': 2,
                'score_improvement': 0,
                'runtime_seconds': 8,
                'consecutive_no_gain_calls': 2,
            },
            'cooling': {
                'calls': 4,
                'score_improvement': 0,
                'runtime_seconds': 16,
                'consecutive_no_gain_calls': 4,
                'cooldown_until_cycle': 8,
            },
        }

        ordered = optimizer._adaptive_repair_order(repairs, stats, cycle=5)

        self.assertEqual(
            [row[0] for row in ordered],
            ['productive', 'cold', 'unproductive'],
        )

    def test_repeated_zero_gain_adds_bounded_exponential_cooldown(self):
        stats = {'runtime_seconds': 0}

        optimizer._record_adaptive_repair_productivity(
            stats, score_gain=0, runtime_seconds=4, cycle=1,
        )
        self.assertNotIn('cooldown_until_cycle', stats)

        optimizer._record_adaptive_repair_productivity(
            stats, score_gain=0, runtime_seconds=4, cycle=2,
        )
        self.assertEqual(stats['cooldown_until_cycle'], 4)

        optimizer._record_adaptive_repair_productivity(
            stats, score_gain=50, runtime_seconds=4, cycle=4,
        )
        self.assertEqual(stats['consecutive_no_gain_calls'], 0)
        self.assertEqual(stats['cooldown_until_cycle'], 4)
        self.assertAlmostEqual(stats['score_improvement_per_second'], 50 / 8.76)

    def test_empty_repairs_receive_immediate_stronger_cooldown(self):
        stats = {'runtime_seconds': 0}

        optimizer._record_adaptive_repair_productivity(
            stats, score_gain=0, runtime_seconds=4, cycle=1, attempts=0,
        )

        self.assertEqual(stats['consecutive_empty_calls'], 1)
        self.assertEqual(stats['consecutive_no_gain_calls'], 1)
        self.assertEqual(stats['cooldown_until_cycle'], 6)

        optimizer._record_adaptive_repair_productivity(
            stats, score_gain=0, runtime_seconds=4, cycle=6, attempts=0,
        )

        self.assertEqual(stats['consecutive_empty_calls'], 2)
        self.assertEqual(stats['cooldown_until_cycle'], 15)

        optimizer._record_adaptive_repair_productivity(
            stats, score_gain=10, runtime_seconds=1, cycle=15, attempts=1,
        )

        self.assertEqual(stats['consecutive_empty_calls'], 0)
        self.assertEqual(stats['consecutive_no_gain_calls'], 0)

    def test_reconstruction_window_padding_uses_configured_temporal_rules(self):
        contracts = {
            1: SimpleNamespace(
                night_settings={
                    'days_off_after_night_block': 4,
                    'max_consecutive_night_shifts': 3,
                },
                workload_settings={'max_days_in_row': 6},
            ),
            2: SimpleNamespace(
                night_settings={'days_off_before_next_night_shift': 12},
                workload_settings={},
            ),
        }

        self.assertEqual(
            optimizer._constraint_boundary_padding_days(contracts), 7,
        )
        self.assertEqual(
            optimizer._constraint_boundary_padding_days({1: contracts[1]}), 6,
        )

    def test_assignment_distance_counts_changed_owners(self):
        self.assertEqual(
            optimizer._state_assignment_distance(
                {1: [10], 2: [20], 3: [30]},
                {1: [20], 2: [10], 3: [30]},
            ),
            2,
        )

    def test_violation_focuses_include_all_configured_score_families(self):
        instances = [
            SimpleNamespace(id=1, date=date(2026, 12, 4)),
            SimpleNamespace(id=2, date=date(2026, 12, 5)),
        ]
        scoring = {
            'workload_score_rows': [{
                'physician_id': 1,
                'rule_rows': [{
                    'period_start': '2026-12-04',
                    'period_end': '2026-12-05',
                    'score_contribution': 80,
                }],
            }],
            'same_shift_violations': [{
                'physician_id': 2,
                'dates_involved': ['2026-12-05'],
                'penalty': 40,
                'violation_type': 'SAME_SHIFT_STREAK',
            }],
        }
        with patch.object(
            optimizer, '_night_violation_report',
            return_value={'night_violations': [{
                'physician_id': 3,
                'dates_involved': ['2026-12-04'],
                'penalty_amount': 60,
                'violation_type': 'MIN_CONSECUTIVE_NIGHTS',
            }]},
        ), patch.object(
            optimizer, '_weekend_volume_report',
            return_value={'violations': [{
                'physician_id': 4,
                'period_start': '2026-12-04',
                'period_end': '2026-12-05',
                'penalty': 20,
                'violation_type': 'WEEKEND_OVER_MAXIMUM',
            }]},
        ), patch.object(
            optimizer, '_request_scoring_rows',
            return_value=[{
                'physician_id': 5,
                'dates_involved': ['2026-12-05'],
                'penalty': 100,
                'violation_type': 'REQUEST_SHIFT_ON_UNMET',
            }],
        ):
            focuses = optimizer._adaptive_violation_focuses(
                instances=instances,
                physicians=[],
                state={1: [], 2: []},
                scoring=scoring,
                contract_by_physician={},
                requests_by_physician_date={},
            )

        self.assertEqual(
            [focus['violation_type'] for focus in focuses],
            [
                'REQUEST_SHIFT_ON_UNMET', 'WORKLOAD',
                'MIN_CONSECUTIVE_NIGHTS', 'SAME_SHIFT_STREAK',
                'WEEKEND_OVER_MAXIMUM',
            ],
        )
        self.assertEqual(focuses[0]['dates'], [date(2026, 12, 5)])

    def test_preserves_repair_order_and_returns_best_valid_state(self):
        calls = []
        budget = FakeBudget()

        def repair(name, improve=False, stop=False):
            def run(**kwargs):
                calls.append(name)
                accepts = []
                if improve:
                    improved_state = {1: [2]}
                    improved_scoring = valid_scoring(4)
                    kwargs['on_improvement'](improved_state, improved_scoring)
                    accepts.append({'score_after': 4})
                if stop:
                    budget.stopped = True
                return kwargs['state'], valid_scoring(10), {
                    'attempts': 1, 'accepts': accepts,
                }
            return run

        def night_spacing(**kwargs):
            if kwargs.get('maximum_only'):
                return repair('night_maximum')(**kwargs)
            return repair('night_spacing', improve=True, stop=True)(**kwargs)

        with patch.object(optimizer, '_repair_weekend_support_swaps',
                          side_effect=repair('weekend_support_swaps')), \
             patch.object(optimizer, '_repair_general_constraint_reassignments',
                          side_effect=repair('general_reassignments')), \
             patch.object(optimizer, '_repair_general_constraint_swaps',
                          side_effect=repair('general_swaps')), \
             patch.object(optimizer, '_repair_workload_transfers',
                          side_effect=repair('workload_transfers')), \
             patch.object(optimizer, '_repair_night_minimum_distribution',
                          side_effect=repair('night_minimum')), \
             patch.object(optimizer, '_repair_night_spacing_swaps',
                          side_effect=night_spacing), \
             patch.object(optimizer, '_repair_recovery_day_swaps',
                          side_effect=repair('night_recovery')):
            state, scoring, debug = optimizer._run_adaptive_search_rounds(
                instances=[SimpleNamespace(id=1, required_staffing=1, is_locked_open=False)],
                physicians=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
                initial_state={1: [1]}, initial_scoring=valid_scoring(10),
                manual_pairs=set(), targets={}, contract_by_physician={},
                requests_by_physician_date={}, eligible_facilities_by_physician={},
                minimum_rest_by_physician={}, search_budget=budget, rng=Random(7),
                debug={'cycles': 0, 'improvements': 0, 'attempts': 0},
            )

        self.assertEqual(calls, [
            'weekend_support_swaps', 'general_reassignments',
            'general_swaps', 'workload_transfers',
            'night_minimum', 'night_maximum', 'night_recovery', 'night_spacing',
        ])
        self.assertEqual(state, {1: [2]})
        self.assertEqual(scoring['score'], Decimal('4'))
        self.assertEqual(debug['cycles'], 1)
        self.assertEqual(debug['improvements'], 1)
        self.assertEqual(debug['attempts'], 8)
        self.assertEqual(list(debug['repair_stats']), calls)

    def test_new_global_best_restarts_repairs_from_best_with_new_seed(self):
        budget = FakeBudget()
        seen_states = []

        def weekend_repair(**kwargs):
            seen_states.append(kwargs['state'])
            if len(seen_states) == 1:
                improved_state = {1: [2]}
                improved_scoring = valid_scoring(4)
                kwargs['on_improvement'](improved_state, improved_scoring)
                return improved_state, improved_scoring, {
                    'attempts': 1,
                    'accepts': [{'score_after': 4}],
                }
            budget.stopped = True
            return kwargs['state'], valid_scoring(4), {
                'attempts': 1,
                'accepts': [],
            }

        with patch.object(
            optimizer, '_repair_weekend_support_swaps',
            side_effect=weekend_repair,
        ):
            state, scoring, debug = optimizer._run_adaptive_search_rounds(
                instances=[SimpleNamespace(
                    id=1, required_staffing=1, is_locked_open=False,
                )],
                physicians=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
                initial_state={1: [1]}, initial_scoring=valid_scoring(10),
                manual_pairs=set(), targets={}, contract_by_physician={},
                requests_by_physician_date={}, eligible_facilities_by_physician={},
                minimum_rest_by_physician={}, search_budget=budget, rng=Random(7),
                debug={'cycles': 0, 'improvements': 0, 'attempts': 0},
            )

        self.assertEqual(seen_states, [{1: [1]}, {1: [2]}])
        self.assertEqual(state, {1: [2]})
        self.assertEqual(scoring['score'], Decimal('4'))
        self.assertEqual(debug['best_chain_restarts'], 1)
        self.assertEqual(len(debug['best_chain_details']), 1)
        self.assertNotEqual(debug['best_chain_details'][0]['seed'], 7)

    def test_new_global_best_preserves_other_repairs_cooldowns(self):
        budget = FakeBudget()
        calls = []

        def general_reassignment(**kwargs):
            calls.append('general_reassignments')
            if len(calls) == 1:
                improved_state = {1: [2]}
                improved_scoring = valid_scoring(4)
                kwargs['on_improvement'](improved_state, improved_scoring)
                return improved_state, improved_scoring, {
                    'attempts': 1,
                    'accepts': [{'score_after': 4}],
                }
            budget.stopped = True
            return kwargs['state'], valid_scoring(4), {
                'attempts': 1,
                'accepts': [],
            }

        cooling_stats = {
            repair_name: {
                'calls': 2,
                'attempts': 2,
                'legal_candidates': 0,
                'scored_candidates': 0,
                'accepts': 0,
                'runtime_seconds': 8,
                'consecutive_no_gain_calls': 2,
                'cooldown_until_cycle': 5,
            }
            for repair_name in (
                'weekend_support_swaps', 'general_swaps',
                'workload_transfers', 'night_minimum', 'night_maximum',
                'night_recovery', 'night_spacing',
            )
        }

        with patch.object(
            optimizer, '_repair_general_constraint_reassignments',
            side_effect=general_reassignment,
        ):
            state, scoring, debug = optimizer._run_adaptive_search_rounds(
                instances=[SimpleNamespace(
                    id=1, required_staffing=1, is_locked_open=False,
                )],
                physicians=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
                initial_state={1: [1]}, initial_scoring=valid_scoring(10),
                manual_pairs=set(), targets={}, contract_by_physician={},
                requests_by_physician_date={}, eligible_facilities_by_physician={},
                minimum_rest_by_physician={}, search_budget=budget, rng=Random(7),
                debug={
                    'cycles': 0,
                    'improvements': 0,
                    'attempts': 0,
                    'repair_stats': cooling_stats,
                },
            )

        self.assertEqual(calls, [
            'general_reassignments', 'general_reassignments',
        ])
        self.assertEqual(state, {1: [2]})
        self.assertEqual(scoring['score'], Decimal('4'))
        self.assertEqual(
            debug['repair_stats']['weekend_support_swaps']['cooldown_until_cycle'],
            5,
        )
        self.assertEqual(
            debug['repair_stats']['weekend_support_swaps']['consecutive_no_gain_calls'],
            2,
        )
