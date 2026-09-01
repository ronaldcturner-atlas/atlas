from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer


def instance(instance_id, *, night, locked=False):
    return SimpleNamespace(
        id=instance_id,
        date=date(2026, 9, instance_id),
        is_locked_open=locked,
        shift_template=SimpleNamespace(night_shift=night),
    )


def scoring(value, *, invalid=False):
    return {
        'score': Decimal(str(value)),
        'validation': {
            'final_overlap_violations': int(invalid),
            'final_rest_violations': 0,
            'final_duplicate_violations': 0,
            'final_overstaffed_violations': 0,
            'final_inactive_physician_violations': 0,
            'final_facility_ineligible_violations': 0,
        },
    }


class NightMinimumRepairTests(SimpleTestCase):
    def setUp(self):
        self.instances = [instance(1, night=True), instance(2, night=False)]
        self.physicians = [SimpleNamespace(id=10), SimpleNamespace(id=20)]
        self.kwargs = dict(
            instances=self.instances, physicians=self.physicians,
            state={1: [10], 2: [20]}, manual_pairs=set(), targets={},
            contract_by_physician={}, requests_by_physician_date={},
            eligible_facilities_by_physician={}, minimum_rest_by_physician={},
            should_stop=lambda: False, candidate_limit=20,
        )
        self.status = {
            'physicians_under_night_minimum': [{
                'physician_id': 20, 'minimum': 1, 'actual': 0,
                'penalty_weight': 100, 'period_start': '2026-09-01',
                'period_end': '2026-09-30',
            }],
            'physicians_over_night_minimum': [{
                'physician_id': 10, 'minimum': 0, 'actual': 1,
                'penalty_weight': 100, 'period_start': '2026-09-01',
                'period_end': '2026-09-30',
            }],
        }

    def test_accepts_verified_legal_direct_reassignment(self):
        with patch.object(optimizer, '_night_minimum_status', return_value=self.status), \
             patch.object(optimizer, '_can_assign_in_state', return_value=True), \
             patch.object(optimizer, '_score_schedule', side_effect=[scoring(100), scoring(20)]):
            state, result, debug = optimizer._repair_night_minimum_distribution(**self.kwargs)
        self.assertEqual(state[1], [20])
        self.assertEqual(result['score'], Decimal('20'))
        self.assertEqual(debug['accepts'][0]['kind'], 'reassignment')

    def test_rejects_worse_or_hard_invalid_candidate(self):
        for candidate in (scoring(100), scoring(20, invalid=True)):
            with self.subTest(candidate=candidate), \
                 patch.object(optimizer, '_night_minimum_status', return_value=self.status), \
                 patch.object(optimizer, '_can_assign_in_state', return_value=True), \
                 patch.object(optimizer, '_score_schedule', side_effect=[scoring(100), candidate, candidate]):
                state, result, debug = optimizer._repair_night_minimum_distribution(**self.kwargs)
            self.assertEqual(state, {1: [10], 2: [20]})
            self.assertEqual(result['score'], Decimal('100'))
            self.assertEqual(debug['accepts'], [])

    def test_preserves_manual_and_locked_nights(self):
        for manual_pairs, locked in (({(1, 10)}, False), (set(), True)):
            with self.subTest(manual=bool(manual_pairs), locked=locked):
                kwargs = dict(self.kwargs)
                kwargs['manual_pairs'] = manual_pairs
                kwargs['instances'] = [instance(1, night=True, locked=locked), instance(2, night=False)]
                with patch.object(optimizer, '_night_minimum_status', return_value=self.status), \
                     patch.object(optimizer, '_score_schedule', return_value=scoring(100)):
                    state, _, debug = optimizer._repair_night_minimum_distribution(**kwargs)
                self.assertEqual(state, {1: [10], 2: [20]})
                self.assertEqual(debug['attempts'], 0)
