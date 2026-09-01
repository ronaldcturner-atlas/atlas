from decimal import Decimal
from random import Random
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer


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

    def reason(self):
        return 'stall_limit' if self.stopped else None

    def observe(self, score, *, valid):
        if not valid or score >= self.best_score:
            return False
        self.best_score = score
        return True


class AdaptiveSearchRoundTests(SimpleTestCase):
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
            return repair('night_spacing', stop=True)(**kwargs)

        with patch.object(optimizer, '_repair_general_constraint_reassignments',
                          side_effect=repair('general_reassignments', improve=True)), \
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
                instances=[SimpleNamespace(id=1, required_staffing=1)],
                physicians=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
                initial_state={1: [1]}, initial_scoring=valid_scoring(10),
                manual_pairs=set(), targets={}, contract_by_physician={},
                requests_by_physician_date={}, eligible_facilities_by_physician={},
                minimum_rest_by_physician={}, search_budget=budget, rng=Random(7),
                debug={'cycles': 0, 'improvements': 0, 'attempts': 0},
            )

        self.assertEqual(calls, [
            'general_reassignments', 'general_swaps', 'workload_transfers',
            'night_minimum', 'night_maximum', 'night_recovery', 'night_spacing',
        ])
        self.assertEqual(state, {1: [2]})
        self.assertEqual(scoring['score'], Decimal('4'))
        self.assertEqual(debug['cycles'], 1)
        self.assertEqual(debug['improvements'], 1)
        self.assertEqual(debug['attempts'], 7)
        self.assertEqual(list(debug['repair_stats']), calls)
