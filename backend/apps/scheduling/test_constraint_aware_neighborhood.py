from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from random import Random
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer
from .models import ScheduleRequest


class FakeRelatedManager:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


def scoring(score, weekend=0):
    return {
        'score': Decimal(str(score)),
        'fixed_request_on_unmet': 0,
        'breakdown': {
            'weekend_score': Decimal(str(weekend)),
        },
        'workload_score_rows': [{
            'physician_id': 1,
            'assigned_hours': 8,
            'score_contribution_exact': '100',
        }],
        'same_shift_violations': [],
    }


class ConstraintAwareNeighborhoodTests(SimpleTestCase):
    def test_neighborhood_stops_before_building_when_deadline_is_reached(self):
        initial = {1: [1]}

        state, result, debug = optimizer._solve_bounded_multi_physician_neighborhood(
            instances=[],
            physicians=[],
            state=initial,
            scoring=scoring(100),
            manual_pairs=set(),
            targets={},
            contract_by_physician={},
            requests_by_physician_date={},
            eligible_facilities_by_physician={},
            minimum_rest_by_physician={},
            rng=Random(1),
            should_stop=lambda: True,
        )

        self.assertEqual(state, initial)
        self.assertEqual(result['score'], Decimal('100'))
        self.assertTrue(debug['stopped'])
        self.assertEqual(debug['stopped_stage'], 'before_model_build')

    def test_weekend_rule_guides_atomic_exchange_before_full_score_acceptance(self):
        facility = SimpleNamespace(id=10)
        weekend_template = SimpleNamespace(
            id=100, night_shift=False, weekend_days=['Saturday'],
        )
        instances = [
            SimpleNamespace(
                id=1, date=date(2026, 12, 5), facility_id=10,
                shift_template_id=100, shift_template=weekend_template,
                start_datetime=datetime(2026, 12, 5, 8, tzinfo=timezone.utc),
                end_datetime=datetime(2026, 12, 5, 16, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                id=2, date=date(2026, 12, 7), facility_id=10,
                shift_template_id=100, shift_template=weekend_template,
                start_datetime=datetime(2026, 12, 7, 8, tzinfo=timezone.utc),
                end_datetime=datetime(2026, 12, 7, 16, tzinfo=timezone.utc),
            ),
        ]
        physicians = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        contracts = {
            1: SimpleNamespace(
                id=1, name='Limited weekends', night_settings={},
                workload_settings={},
                weekend_settings={'period_rules': [{
                    'period_type': 'SCHEDULE_BLOCK',
                    'max_volume': 0,
                    'max_penalty_weight': 100,
                }]},
            ),
            2: SimpleNamespace(
                id=2, name='No weekend limit', night_settings={},
                workload_settings={}, weekend_settings={},
            ),
        }
        initial = {1: [1], 2: [2]}

        def authoritative_score(_instances, _physicians, state, *_args, **_kwargs):
            return scoring(0 if state.get(1) == [2] else 100)

        with patch.object(
            optimizer, '_night_violation_report',
            return_value={'night_violations': []},
        ), patch.object(
            optimizer, '_request_scoring_rows', return_value=[],
        ), patch.object(
            optimizer, '_score_schedule', side_effect=authoritative_score,
        ):
            state, result, debug = optimizer._solve_bounded_multi_physician_neighborhood(
                instances=instances, physicians=physicians,
                state=initial, scoring=scoring(100, weekend=100),
                manual_pairs=set(), targets={}, contract_by_physician=contracts,
                requests_by_physician_date={},
                eligible_facilities_by_physician={1: {10}, 2: {10}},
                minimum_rest_by_physician={1: 0, 2: 0},
                rng=Random(7), focus_physician_ids={1},
                focus_start=date(2026, 12, 1), focus_end=date(2026, 12, 31),
                cohort_size=2,
            )

        self.assertTrue(debug['accepted'], debug)
        self.assertEqual(state, {1: [2], 2: [2]})
        self.assertEqual(result['score'], Decimal('0'))
        self.assertIn(2, debug['connected_owner_ids'])

    def test_focus_instances_include_owners_for_atomic_request_block_exchange(self):
        day_template = SimpleNamespace(
            id=100, night_shift=False, weekend_days=[],
        )
        night_template = SimpleNamespace(
            id=200, night_shift=True, weekend_days=[],
        )
        instances = []
        for offset in range(2):
            day = date(2027, 2, 11) + timedelta(days=offset)
            instances.extend([
                SimpleNamespace(
                    id=offset * 2 + 1, date=day, facility_id=10,
                    shift_template_id=night_template.id,
                    shift_template=night_template,
                    start_datetime=datetime.combine(
                        day, time(22), tzinfo=timezone.utc,
                    ),
                    end_datetime=datetime.combine(
                        day + timedelta(days=1), time(6), tzinfo=timezone.utc,
                    ),
                ),
                SimpleNamespace(
                    id=offset * 2 + 2, date=day, facility_id=10,
                    shift_template_id=day_template.id,
                    shift_template=day_template,
                    start_datetime=datetime.combine(
                        day, time(6), tzinfo=timezone.utc,
                    ),
                    end_datetime=datetime.combine(
                        day, time(14), tzinfo=timezone.utc,
                    ),
                ),
            ])
        physicians = [
            SimpleNamespace(id=1),
            SimpleNamespace(id=2),
            SimpleNamespace(id=3),
        ]
        contracts = {
            physician.id: SimpleNamespace(
                id=physician.id, name='Contract', request_settings={
                    'weight_fixed': '50000',
                }, night_settings={}, workload_settings={},
                weekend_settings={}, shift_settings={},
                manual_assignment_only=False,
            )
            for physician in physicians
        }
        requests = defaultdict(list)
        for index, day in enumerate((date(2027, 2, 11), date(2027, 2, 12))):
            requests[1, day].append(SimpleNamespace(
                id=index + 1,
                physician_id=1,
                date=day,
                request_type=ScheduleRequest.RequestType.SHIFT_ON,
                request_scope=ScheduleRequest.RequestScope.ADMIN,
                weight=ScheduleRequest.Weight.FIXED,
                shift_templates=FakeRelatedManager([night_template]),
            ))
        initial = {1: [2], 2: [1], 3: [3], 4: [1]}

        def authoritative_score(_instances, _physicians, state, *_args, **_kwargs):
            unmet = sum(1 for instance_id in (1, 3) if 1 not in state[instance_id])
            return {
                **scoring(unmet * 50000),
                'fixed_request_on_unmet': unmet,
                'validation': {
                    'final_overlap_violations': 0,
                    'final_rest_violations': 0,
                    'final_duplicate_violations': 0,
                    'final_overstaffed_violations': 0,
                    'final_inactive_physician_violations': 0,
                    'final_facility_ineligible_violations': 0,
                },
            }

        with patch.object(
            optimizer, '_night_violation_report',
            return_value={'night_violations': []},
        ), patch.object(
            optimizer, '_request_scoring_rows', return_value=[],
        ), patch.object(
            optimizer, '_score_schedule', side_effect=authoritative_score,
        ):
            state, result, debug = optimizer._solve_bounded_multi_physician_neighborhood(
                instances=instances,
                physicians=physicians,
                state=initial,
                scoring=authoritative_score(None, None, initial),
                manual_pairs=set(),
                targets={1: {'rules': []}, 2: {'rules': []}, 3: {'rules': []}},
                contract_by_physician=contracts,
                requests_by_physician_date=requests,
                eligible_facilities_by_physician={1: {10}, 2: {10}, 3: {10}},
                minimum_rest_by_physician={1: 11, 2: 11, 3: 11},
                rng=Random(17),
                focus_physician_ids={1},
                focus_instance_ids={1, 3},
                focus_start=date(2027, 2, 11),
                focus_end=date(2027, 2, 12),
                cohort_size=3,
            )

        self.assertTrue(debug['accepted'], debug)
        self.assertEqual(result['fixed_request_on_unmet'], 0)
        self.assertEqual(state[1], [1])
        self.assertEqual(state[3], [1])
        self.assertTrue({2, 3}.issubset(set(debug['connected_owner_ids'])))
