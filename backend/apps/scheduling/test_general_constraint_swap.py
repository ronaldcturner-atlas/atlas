from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer


class GeneralConstraintSwapTests(SimpleTestCase):
    def instances(self):
        first_start = datetime(2026, 1, 1, 8)
        second_start = datetime(2026, 1, 2, 8)
        return [
            SimpleNamespace(id=1, date=date(2026, 1, 1), start_datetime=first_start,
                            end_datetime=first_start + timedelta(hours=8)),
            SimpleNamespace(id=2, date=date(2026, 1, 2), start_datetime=second_start,
                            end_datetime=second_start + timedelta(hours=8)),
        ]

    @staticmethod
    def scoring(_instances, _physicians, state, *_args, **_kwargs):
        swapped = state[1] == [2] and state[2] == [1]
        return {'score': Decimal('3') if swapped else Decimal('10')}

    def run_repair(self, *, manual_pairs=None, can_assign=True, **options):
        improvements = []
        with patch.object(optimizer, '_score_schedule', side_effect=self.scoring), \
             patch.object(optimizer, '_request_repair_candidates', return_value=[(1, 1)]), \
             patch.object(optimizer, '_same_shift_break_candidates', return_value=[]), \
             patch.object(optimizer, '_consecutive_day_break_candidates', return_value=[]), \
             patch.object(optimizer, '_can_assign_in_state', return_value=can_assign):
            result = optimizer._repair_general_constraint_swaps(
                instances=self.instances(),
                physicians=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
                state={1: [1], 2: [2]}, manual_pairs=manual_pairs or set(),
                targets={}, contract_by_physician={}, requests_by_physician_date={},
                eligible_facilities_by_physician={}, minimum_rest_by_physician={},
                on_improvement=lambda state, score: improvements.append((state, score)),
                **options,
            )
        return (*result, improvements)

    def test_accepts_strict_total_improvement(self):
        state, scoring, debug, improvements = self.run_repair()
        self.assertEqual(state, {1: [2], 2: [1]})
        self.assertEqual(scoring['score'], Decimal('3'))
        self.assertEqual(debug['attempts'], 1)
        self.assertEqual(debug['accepts'][0]['source'], 'request')
        self.assertEqual(len(improvements), 1)

    def test_preserves_manual_assignments_and_hard_assignment_rules(self):
        for manual_pairs, can_assign in (({(1, 1)}, True), ({(2, 2)}, True), (set(), False)):
            with self.subTest(manual_pairs=manual_pairs, can_assign=can_assign):
                state, scoring, debug, improvements = self.run_repair(
                    manual_pairs=manual_pairs, can_assign=can_assign)
                self.assertEqual(state, {1: [1], 2: [2]})
                self.assertEqual(scoring['score'], Decimal('10'))
                self.assertFalse(debug['accepts'])
                self.assertFalse(improvements)

    def test_respects_stop_and_candidate_limits(self):
        for options in ({'candidate_limit': 0}, {'should_stop': lambda: True}):
            with self.subTest(options=options):
                state, _scoring, debug, _improvements = self.run_repair(**options)
                self.assertEqual(state, {1: [1], 2: [2]})
                self.assertEqual(debug['stopped_reason'], 'runtime_or_candidate_limit')

