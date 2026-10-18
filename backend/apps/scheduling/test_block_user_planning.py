from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase

from .block_user_planning import (
    apply_manual_temporary_overrides,
    block_workload_override,
    generated_partial_settings,
)
from .models import Contract


class BlockUserPlanningTests(SimpleTestCase):
    def setUp(self):
        self.block = SimpleNamespace(
            start_date=date(2027, 2, 1),
            end_date=date(2027, 2, 28),
        )
        self.contract = Contract(
            workload_settings={
                'period_rules': [{
                    'period_type': 'MONTH',
                    'units': 'HOURS',
                    'min_value': '100',
                    'max_value': '140',
                    'min_penalty_weight': '9000',
                    'max_penalty_weight': '10000',
                }],
            },
            facility_settings={},
            shift_settings={},
            night_settings={},
            weekend_settings={},
            request_settings={},
        )

    def test_partial_month_prorates_volume_bounds_and_preserves_penalties(self):
        unavailable = [
            date(2027, 2, day).isoformat()
            for day in range(15, 29)
        ]

        settings, adjusted = generated_partial_settings(
            self.contract, self.block, unavailable,
        )
        rule = settings['workload_settings']['period_rules'][0]

        self.assertEqual(rule['period_type'], 'SCHEDULE_BLOCK')
        self.assertEqual(rule['min_value'], '50')
        self.assertEqual(rule['max_value'], '70')
        self.assertEqual(rule['min_penalty_weight'], '9000')
        self.assertEqual(rule['max_penalty_weight'], '10000')
        self.assertIn('workload_settings.period_rules.0.min_value', adjusted)
        self.assertIn('workload_settings.period_rules.0.max_value', adjusted)

    def test_manual_temporary_override_changes_only_selected_rule_value(self):
        settings, _adjusted = generated_partial_settings(
            self.contract, self.block, [],
        )
        updated, overrides = apply_manual_temporary_overrides(
            self.contract,
            settings,
            {'workload_settings.period_rules.0.max_value': '125'},
        )

        rule = updated['workload_settings']['period_rules'][0]
        self.assertEqual(rule['max_value'], '125')
        self.assertEqual(rule['min_value'], '100')
        self.assertEqual(
            overrides['workload_settings.period_rules.0.max_value'], '125',
        )

    def test_block_workload_override_uses_shared_hours_and_shifts_values(self):
        plan = {
            'minimum_hours': '295',
            'maximum_hours': '305',
            'minimum_shifts': '28',
            'maximum_shifts': '30',
        }

        self.assertEqual(
            block_workload_override(plan, 'HOURS'),
            {'minimum': 295, 'maximum': 305},
        )
        self.assertEqual(
            block_workload_override(plan, 'SHIFTS'),
            {'minimum': 28, 'maximum': 30},
        )
