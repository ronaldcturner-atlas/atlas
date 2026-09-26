from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer as o


class PenaltyScopeTests(SimpleTestCase):
    def test_boundary_context_shift_is_not_a_current_repair_target(self):
        violation = {
            'physician_id': 7,
            'shift_instance_ids': [25562, 100, 101],
        }
        state = {100: [7], 101: [7, 8]}

        self.assertEqual(
            o._current_violation_assignment_pairs(
                violation, state, {(101, 7)},
            ),
            [(100, 7)],
        )

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

    def make_assignment_instance(self, day, *, instance_id, night=False, start_hour=7, hours=8):
        template = SimpleNamespace(
            id=10,
            name='Night' if night else 'Day',
            weekend_days=['Saturday', 'Sunday'],
            night_shift=night,
        )
        facility = SimpleNamespace(id=20, name='Test Facility', short_name='Test')
        return SimpleNamespace(
            id=instance_id,
            date=day,
            facility=facility,
            facility_id=facility.id,
            shift_template=template,
            shift_template_id=template.id,
            start_datetime=datetime.combine(day, datetime.min.time()) + timedelta(hours=start_hour),
            end_datetime=datetime.combine(day, datetime.min.time()) + timedelta(hours=start_hour + hours),
            required_staffing=1,
        )

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

    def test_weekend_shift_streak_continues_from_published_context(self):
        current = self.make_instances([date(2026, 11, 1)])[0]
        prior = self.make_instances([date(2026, 10, 31)])[0]
        prior.id = -1
        current._published_weekend_context = {1: (prior,)}
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=10,
            name='Boundary weekend shifts',
            weekend_settings={
                'min_consecutive_weekend_shifts': 2,
                'min_consecutive_weekend_shifts_penalty_weight': 100,
            },
        )

        report = o._weekend_volume_report(
            [current],
            [physician],
            {current.id: [physician.id]},
            {physician.id: contract},
            details=True,
        )

        self.assertEqual(report, {'score': 0, 'violations': []})

    def test_weekend_shift_streak_without_prior_assignment_is_penalized(self):
        current = self.make_instances([date(2026, 11, 1)])[0]
        current._published_weekend_context = {}
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=11,
            name='Boundary weekend shifts',
            weekend_settings={
                'min_consecutive_weekend_shifts': 2,
                'min_consecutive_weekend_shifts_penalty_weight': 100,
            },
        )

        report = o._weekend_volume_report(
            [current],
            [physician],
            {current.id: [physician.id]},
            {physician.id: contract},
            details=True,
        )

        self.assertEqual(report['score'], 100)
        self.assertEqual(
            report['violations'][0]['violation_type'],
            'MIN_CONSECUTIVE_WEEKEND_SHIFTS',
        )

    def test_consecutive_weekends_include_prior_published_streak(self):
        current = self.make_instances([date(2026, 11, 1)])[0]
        prior = self.make_instances([
            date(2026, 10, 10),
            date(2026, 10, 17),
            date(2026, 10, 24),
        ])
        for index, instance in enumerate(prior, start=1):
            instance.id = -index
        current._published_weekend_context = {1: tuple(prior)}
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=12,
            name='Cross-block weekend maximum',
            weekend_settings={
                'max_consecutive_weekends': 3,
                'max_consecutive_weekends_penalty_weight': 100,
            },
        )

        report = o._weekend_volume_report(
            [current],
            [physician],
            {current.id: [physician.id]},
            {physician.id: contract},
            details=True,
        )

        self.assertEqual(report['score'], 100)
        self.assertEqual(
            report['violations'][0]['violation_type'],
            'MAX_CONSECUTIVE_WEEKENDS',
        )
        self.assertEqual(report['violations'][0]['actual_value'], 4)

    def test_prior_weekend_excess_is_not_recharged_in_new_block(self):
        current = self.make_instances([date(2026, 11, 1)])[0]
        prior = self.make_instances([
            date(2026, 10, 3),
            date(2026, 10, 10),
            date(2026, 10, 17),
            date(2026, 10, 24),
        ])
        for index, instance in enumerate(prior, start=1):
            instance.id = -index
        current._published_weekend_context = {1: tuple(prior)}
        current._published_boundary_context = {1: tuple(prior)}
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=13,
            name='Incremental weekend maximum',
            weekend_settings={
                'max_consecutive_weekends': 3,
                'max_consecutive_weekends_penalty_weight': 100,
            },
        )

        report = o._weekend_volume_report(
            [current], [physician], {current.id: [physician.id]},
            {physician.id: contract}, details=True,
        )

        self.assertEqual(report['score'], 100)
        self.assertEqual(len(report['violations']), 1)

    def test_prior_friday_night_informs_new_block_weekend_off_rule(self):
        saturday, sunday = self.make_instances([
            date(2026, 11, 7), date(2026, 11, 8),
        ])
        schedule_block = SimpleNamespace(
            start_date=saturday.date,
            end_date=sunday.date,
        )
        saturday.schedule_block = schedule_block
        sunday.schedule_block = schedule_block
        prior_friday = self.make_assignment_instance(
            date(2026, 11, 6), instance_id=-1, night=True, start_hour=22,
        )
        saturday._published_weekend_context = {}
        saturday._published_boundary_context = {1: (prior_friday,)}
        saturday._published_boundary_date_range = (
            date(2026, 9, 1), date(2026, 11, 6),
        )
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(
            id=14,
            name='Friday boundary',
            weekend_settings={
                'block_friday_night_before_weekend_off': True,
                'block_friday_night_before_weekend_off_penalty_weight': 500,
            },
        )

        report = o._weekend_volume_report(
            [saturday, sunday], [physician], {}, {physician.id: contract},
            details=True,
        )

        self.assertEqual(report['score'], 500)
        self.assertEqual(
            report['violations'][0]['violation_type'],
            'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF',
        )

    def test_night_report_validation_accepts_verified_prior_live_assignment(self):
        prior = self.make_assignment_instance(
            date(2026, 10, 30), instance_id=-1, night=True, start_hour=22,
        )
        current = self.make_assignment_instance(
            date(2026, 11, 6), instance_id=1, night=True, start_hour=22,
        )
        prior.schedule_version_id = 90
        current.schedule_version_id = 100
        physician_id = 1
        assignment = SimpleNamespace(
            shift_instance_id=current.id,
            physician_id=physician_id,
            shift_instance=current,
        )
        version = SimpleNamespace(
            id=100,
            schedule_block=SimpleNamespace(
                start_date=date(2026, 11, 1),
                end_date=date(2027, 1, 31),
            ),
        )
        violation = {
            'physician_id': physician_id,
            'violation_type': 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK',
            'shift_instance_ids': [prior.id, current.id],
            'night_block_assignments': [
                {'shift_instance_id': prior.id},
                {'shift_instance_id': current.id},
            ],
        }

        filtered = o._validated_night_report_for_current_assignments(
            {'night_violations': [violation]},
            version,
            [assignment],
            boundary_context={physician_id: (prior,)},
        )

        self.assertEqual(filtered['night_violations'], [violation])
        self.assertEqual(filtered['stale_violation_rows_dropped'], 0)
        self.assertEqual(filtered['violation_assignment_validation_errors'], [])

    def test_boundary_assignment_blocks_insufficient_rest_in_new_block(self):
        prior = self.make_assignment_instance(
            date(2026, 10, 31), instance_id=-1, night=True, start_hour=22,
        )
        current = self.make_assignment_instance(
            date(2026, 11, 1), instance_id=1, start_hour=6,
        )
        current._published_boundary_context = {1: (prior,)}

        validation = o._validate_schedule(
            [current],
            [SimpleNamespace(id=1, active=True)],
            {current.id: [1]},
            {1: {current.facility_id}},
            {1: 10},
        )

        self.assertEqual(validation['final_overlap_violations'], 0)
        self.assertEqual(validation['final_rest_violations'], 1)

    def test_prior_nights_extend_sequence_but_do_not_enter_new_block_volume(self):
        prior = self.make_assignment_instance(
            date(2026, 10, 31), instance_id=-1, night=True, start_hour=22,
        )
        current = self.make_assignment_instance(
            date(2026, 11, 1), instance_id=1, night=True, start_hour=22,
        )
        current._published_boundary_context = {1: (prior,)}
        physician = SimpleNamespace(id=1, display_name='Boundary Physician')
        contract = SimpleNamespace(
            id=21,
            name='Boundary Nights',
            night_settings={
                'min_consecutive_night_shifts': 2,
                'min_consecutive_night_shifts_penalty_weight': 100,
                'period_rules': [{
                    'period_type': 'MONTH',
                    'max_shifts': 1,
                    'max_penalty_weight': 1000,
                }],
            },
        )

        report = o._night_violation_report(
            [current], [physician], {current.id: [1]}, {1: contract},
        )

        self.assertEqual(report['score'], 0)
        self.assertEqual(report['night_shifts_by_physician'][0]['night_shifts'], 1)

    def test_prior_night_excess_is_not_recharged_but_recovery_is_enforced(self):
        prior = [
            self.make_assignment_instance(
                date(2026, 10, 26) + timedelta(days=index),
                instance_id=-(index + 1),
                night=True,
                start_hour=22,
            )
            for index in range(6)
        ]
        current = self.make_assignment_instance(
            date(2026, 11, 1), instance_id=1, night=False, start_hour=12,
        )
        current._published_boundary_context = {1: tuple(prior)}
        physician = SimpleNamespace(id=1, display_name='Boundary Physician')
        contract = SimpleNamespace(
            id=22,
            name='Boundary Recovery',
            night_settings={
                'max_consecutive_night_shifts': 4,
                'max_consecutive_night_shifts_penalty_weight': 1000,
                'days_off_after_night_block': 2,
                'days_off_after_night_block_penalty_weight': 100,
            },
        )

        report = o._night_violation_report(
            [current], [physician], {current.id: [1]}, {1: contract},
        )
        types = [row['violation_type'] for row in report['night_violations']]

        self.assertNotIn('MAX_CONSECUTIVE_NIGHTS', types)
        self.assertEqual(types, ['INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT'])
        self.assertEqual(report['score'], 200)

    def test_boundary_consecutive_days_charges_only_new_incremental_excess(self):
        prior = [
            self.make_assignment_instance(
                date(2026, 10, 27) + timedelta(days=index),
                instance_id=-(index + 1),
            )
            for index in range(5)
        ]
        current = self.make_assignment_instance(date(2026, 11, 1), instance_id=1)
        current._published_boundary_context = {1: tuple(prior)}
        physician = SimpleNamespace(id=1, display_name='Boundary Physician')
        contract = SimpleNamespace(
            id=23,
            name='Boundary Days',
            workload_settings={
                'max_days_in_row': 5,
                'max_days_in_row_penalty_weight': 100,
            },
        )

        with patch.object(o, '_same_shift_violation_report', return_value=(0, [])), patch.object(
            o, '_shift_period_report', return_value={'score': 0},
        ), patch.object(o, '_weekend_volume_report', return_value={'score': 0}), patch.object(
            o, '_night_violation_report', return_value={'score': 0},
        ):
            scores = o._distribution_score(
                [current], [physician], {current.id: [1]}, {1: contract}, {1: {20}},
            )

        self.assertEqual(scores['consecutive_days_score'], 100)
