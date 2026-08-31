from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer


class GeneralConstraintRepairTests(SimpleTestCase):
    def scoring(self, _instances, _physicians, state, *_args, **_kwargs):
        # Moving the assignment from physician 1 to physician 2 is better.
        return {'score': Decimal('10') if state[1] == [1] else Decimal('4')}

    def test_accepts_total_improvement_and_reports_source(self):
        instance = SimpleNamespace(id=1)
        physicians = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        improvements = []
        with patch.object(optimizer, '_score_schedule', side_effect=self.scoring), \
             patch.object(optimizer, '_request_repair_candidates', return_value=[(1, 1)]), \
             patch.object(optimizer, '_same_shift_break_candidates', return_value=[]), \
             patch.object(optimizer, '_consecutive_day_break_candidates', return_value=[]), \
             patch.object(optimizer, '_can_assign_in_state', return_value=True):
            state, scoring, debug = optimizer._repair_general_constraint_reassignments(
                instances=[instance], physicians=physicians, state={1: [1]}, manual_pairs=set(),
                targets={}, contract_by_physician={}, requests_by_physician_date={},
                eligible_facilities_by_physician={}, minimum_rest_by_physician={},
                on_improvement=lambda state, score: improvements.append((state, score)))
        self.assertEqual(state[1], [2])
        self.assertEqual(scoring['score'], Decimal('4'))
        self.assertEqual(debug['accepts'][0]['source'], 'request')
        self.assertEqual(len(improvements), 1)

    def test_rejects_worse_move_and_never_moves_manual_assignment(self):
        instance = SimpleNamespace(id=1)
        physicians = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        def worse(_instances, _physicians, state, *_args, **_kwargs):
            return {'score': Decimal('10') if state[1] == [1] else Decimal('11')}
        for manual_pairs in (set(), {(1, 1)}):
            with self.subTest(manual=bool(manual_pairs)), \
                 patch.object(optimizer, '_score_schedule', side_effect=worse), \
                 patch.object(optimizer, '_request_repair_candidates', return_value=[(1, 1)]), \
                 patch.object(optimizer, '_same_shift_break_candidates', return_value=[(1, 1)]), \
                 patch.object(optimizer, '_consecutive_day_break_candidates', return_value=[(1, 1)]), \
                 patch.object(optimizer, '_can_assign_in_state', return_value=True):
                state, scoring, debug = optimizer._repair_general_constraint_reassignments(
                    instances=[instance], physicians=physicians, state={1: [1]},
                    manual_pairs=manual_pairs, targets={}, contract_by_physician={},
                    requests_by_physician_date={}, eligible_facilities_by_physician={},
                    minimum_rest_by_physician={})
            self.assertEqual(state[1], [1])
            self.assertEqual(scoring['score'], Decimal('10'))
            self.assertFalse(debug['accepts'])

    def test_respects_candidate_and_stop_limits(self):
        instance = SimpleNamespace(id=1)
        physicians = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        with patch.object(optimizer, '_score_schedule', side_effect=self.scoring), \
             patch.object(optimizer, '_request_repair_candidates', return_value=[(1, 1)]), \
             patch.object(optimizer, '_same_shift_break_candidates', return_value=[]), \
             patch.object(optimizer, '_consecutive_day_break_candidates', return_value=[]), \
             patch.object(optimizer, '_can_assign_in_state', return_value=True):
            for options in ({'candidate_limit': 0}, {'should_stop': lambda: True}):
                state, _score, debug = optimizer._repair_general_constraint_reassignments(
                    instances=[instance], physicians=physicians, state={1: [1]}, manual_pairs=set(),
                    targets={}, contract_by_physician={}, requests_by_physician_date={},
                    eligible_facilities_by_physician={}, minimum_rest_by_physician={}, **options)
                self.assertEqual(state[1], [1])
                self.assertEqual(debug['stopped_reason'], 'runtime_or_candidate_limit')
