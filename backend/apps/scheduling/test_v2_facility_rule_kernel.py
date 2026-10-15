from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
from django.test import SimpleTestCase

from .management.commands.benchmark_v2_swap_kernel import (
    _pair_shift_rule_deltas,
    _prepare_shift_rule_kernel,
    _structural_focus_family,
)


def _instance(identifier, facility_id, shift_template_id, day, hours=12):
    started = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return SimpleNamespace(
        id=identifier,
        date=day,
        facility_id=facility_id,
        shift_template_id=shift_template_id,
        start_datetime=started,
        end_datetime=started + timedelta(hours=hours),
    )


def _contract(*, facility_settings=None, shift_settings=None):
    return SimpleNamespace(
        facility_settings=facility_settings or {},
        shift_settings=shift_settings or {},
    )


class V2FacilityRuleKernelTests(SimpleTestCase):
    def _swap_delta(self, instances, contracts):
        # Each physician owns one assignment. Swapping assignment 0 with 1
        # gives physician 2 the first shift and physician 1 the second.
        occupancy = np.asarray([
            [True, False],
            [False, True],
        ], dtype=np.bool_)
        shift_for_assignment = np.asarray([0, 1], dtype=np.int32)
        physician_for_assignment = np.asarray([0, 1], dtype=np.int32)
        kernel = _prepare_shift_rule_kernel(
            instances,
            occupancy,
            [1, 2],
            contracts,
            shift_for_assignment,
            physician_for_assignment,
        )
        return float(_pair_shift_rule_deltas(
            kernel,
            shift_for_assignment,
            physician_for_assignment,
            np.asarray([0], dtype=np.int32),
            np.asarray([1], dtype=np.int32),
        )[0])

    def test_run_151_facility_minimum_is_compiled_into_swap_delta(self):
        instances = [
            _instance(10, 1, 100, date(2027, 3, 24)),
            _instance(20, 4, 200, date(2027, 4, 27)),
        ]
        contracts = {
            1: _contract(),
            2: _contract(
                shift_settings={'rules': [{
                    'shift_template_ids': [100],
                    'period_rules': [{
                        'period_type': 'SCHEDULE_BLOCK',
                        'units': 'SHIFTS',
                        'min_value': 1,
                        'max_value': None,
                        'min_penalty_weight': 20000,
                        'max_penalty_weight': 0,
                    }],
                }]},
                facility_settings={'rules': [{
                    'facility_id': 1,
                    'period_rules': [{
                        'period_type': 'SCHEDULE_BLOCK',
                        'units': 'SHIFTS',
                        'min_value': 1,
                        'max_value': None,
                        'min_penalty_weight': 1000,
                        'max_penalty_weight': 0,
                    }],
                }]},
            ),
        }

        # This is the exact shape of Runs 151 and 152: the existing compiled
        # shift rule supplied -20,000 while the formerly omitted Facility rule
        # supplied the additional -1,000 seen by the authoritative scorer.
        self.assertEqual(self._swap_delta(instances, contracts), -21000.0)

    def test_facility_hours_and_user_effective_penalty_are_compiled(self):
        instances = [
            _instance(10, 1, 100, date(2027, 3, 24), hours=12),
            _instance(20, 4, 200, date(2027, 4, 27), hours=8),
        ]
        # Effective contracts already contain sparse per-user overrides. A
        # user-specific 250-point weight must flow through unchanged.
        contracts = {
            1: _contract(),
            2: _contract(facility_settings={'rules': [{
                'facility_id': 1,
                'period_rules': [{
                    'period_type': 'SCHEDULE_BLOCK',
                    'units': 'HOURS',
                    'min_value': 12,
                    'max_value': None,
                    'min_penalty_weight': 250,
                    'max_penalty_weight': 0,
                }],
            }]}),
        }

        self.assertEqual(self._swap_delta(instances, contracts), -3000.0)

    def test_facility_violations_use_shift_rule_focus_without_database_work(self):
        self.assertEqual(
            _structural_focus_family('FACILITY_RULE_UNDER_MINIMUM'),
            'shift_rule',
        )
        self.assertEqual(
            _structural_focus_family('FACILITY_RULE_OVER_MAXIMUM'),
            'shift_rule',
        )
