from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer as o


class PenaltyScopeTests(SimpleTestCase):
    def test_authoritative_distribution_score_excludes_internal_night_guidance(self):
        with patch.object(
            o, '_same_shift_violation_report', return_value=(0, []),
        ), patch.object(
            o, '_shift_period_report', return_value={'score': 0},
        ), patch.object(
            o, '_weekend_volume_report', return_value={'score': 0},
        ), patch.object(
            o, '_night_violation_report', return_value={'score': 0},
        ) as night_report:
            o._distribution_score(
                [], [], {}, {}, {}, include_internal_night_heuristics=True,
            )

        self.assertFalse(
            night_report.call_args.kwargs['include_internal_heuristics'],
        )

    def make_instances(self, dates, template_id=10, hours=8):
        block = SimpleNamespace(start_date=min(dates), end_date=max(dates))
        template = SimpleNamespace(
            id=template_id,
            weekend_days=['Saturday', 'Sunday'],
            night_shift=False,
        )
        return [
            SimpleNamespace(
                id=index,
                date=day,
                schedule_block=block,
                shift_template=template,
                shift_template_id=template_id,
                start_datetime=datetime.combine(day, datetime.min.time()),
                end_datetime=datetime.combine(day, datetime.min.time()) + timedelta(hours=hours),
            )
            for index, day in enumerate(dates)
        ]

    def test_period_windows_are_calendar_week_calendar_month_and_whole_block(self):
        instances = self.make_instances([
            date(2026, 12, 30),
            date(2026, 12, 31),
            date(2027, 1, 1),
            date(2027, 1, 3),
        ])
        self.assertEqual(
            o._period_windows(instances, 'WEEK'),
            [(date(2026, 12, 30), date(2027, 1, 3))],
        )
        self.assertEqual(
            o._period_windows(instances, 'MONTH'),
            [
                (date(2026, 12, 30), date(2026, 12, 31)),
                (date(2027, 1, 1), date(2027, 1, 3)),
            ],
        )
        self.assertEqual(
            o._period_windows(instances, 'SCHEDULE_BLOCK'),
            [(date(2026, 12, 30), date(2027, 1, 3))],
        )

    def test_shift_group_month_and_schedule_block_rules_score_independently(self):
        instances = self.make_instances([
            date(2026, 12, 5),
            date(2027, 1, 2),
        ])
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=7,
            name='Scoped shift rules',
            shift_settings={
                'rules': [{
                    'label': 'Premium shifts',
                    'shift_template_ids': [10],
                    'period_rules': [
                        {
                            'period_type': 'MONTH',
                            'units': 'SHIFTS',
                            'max_value': 1,
                            'max_penalty_weight': 100,
                        },
                        {
                            'period_type': 'SCHEDULE_BLOCK',
                            'units': 'SHIFTS',
                            'max_value': 1,
                            'max_penalty_weight': 1000,
                        },
                    ],
                }],
            },
        )
        report = o._shift_period_report(
            instances,
            [physician],
            {instance.id: [physician.id] for instance in instances},
            {physician.id: contract},
            details=True,
        )
        self.assertEqual(report['score'], 1000)
        self.assertEqual(len(report['violations']), 1)
        self.assertEqual(report['violations'][0]['period_type'], 'SCHEDULE_BLOCK')
        self.assertEqual(report['violations'][0]['shift_rule_label'], 'Premium shifts')

    def test_shift_group_week_rule_uses_calendar_week_not_rolling_seven_days(self):
        instances = self.make_instances([
            date(2026, 12, 6),  # Sunday
            date(2026, 12, 7),  # Monday, a new calendar week
        ])
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=8,
            name='Weekly shift rules',
            shift_settings={
                'rules': [{
                    'label': 'Weekly group',
                    'shift_template_ids': [10],
                    'period_rules': [{
                        'period_type': 'WEEK',
                        'units': 'SHIFTS',
                        'max_value': 1,
                        'max_penalty_weight': 500,
                    }],
                }],
            },
        )
        report = o._shift_period_report(
            instances,
            [physician],
            {instance.id: [physician.id] for instance in instances},
            {physician.id: contract},
            details=True,
        )
        self.assertEqual(report['score'], 0)
        self.assertEqual(report['violations'], [])

    def test_unconfigured_shift_group_has_no_implicit_penalty(self):
        instances = self.make_instances([date(2026, 12, 5)])
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=9,
            name='No shift rules',
            shift_settings={},
        )
        self.assertEqual(
            o._shift_period_report(
                instances, [physician], {0: [1]}, {1: contract}, details=True,
            ),
            {'score': 0, 'violations': []},
        )
