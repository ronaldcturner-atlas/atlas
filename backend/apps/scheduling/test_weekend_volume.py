from collections import defaultdict
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from . import optimizer as o
from .management.commands.benchmark_optimizer_scale import Command, SCALE_PROFILES
from .models import ContractUserAssignment, OptimizerRun, ScheduleShiftAssignment


class WeekendVolumeTests(SimpleTestCase):
    def report(self, dates, rules, assigned=None, weekend_days=None):
        block = SimpleNamespace(start_date=date(2026, 12, 1), end_date=date(2027, 1, 31))
        template = SimpleNamespace(weekend_days=weekend_days or ['Saturday', 'Sunday'])
        instances = [SimpleNamespace(id=i, date=day, schedule_block=block, shift_template=template)
                     for i, day in enumerate(dates)]
        physician = SimpleNamespace(id=1)
        contract = SimpleNamespace(id=1, name='Test', weekend_settings={'period_rules': rules})
        state = {i: [1] if assigned is None or i in assigned else [] for i in range(len(dates))}
        return o._weekend_volume_report(instances, [physician], state, {1: contract}, details=True)

    def rule(self, period='MONTH', minimum=0, maximum=1, weight=10):
        return dict(period_type=period, min_volume=minimum, max_volume=maximum,
                    min_penalty_weight=weight, max_penalty_weight=weight)

    def test_monthly_maximum_not_applied_to_whole_block(self):
        result = self.report([date(2026, 12, 5), date(2027, 1, 2)], [self.rule()])
        self.assertEqual(result['score'], 0)

    def test_week_month_and_block_all_enforced(self):
        result = self.report([date(2026, 12, 5), date(2026, 12, 6), date(2027, 1, 2)],
                             [self.rule('WEEK'), self.rule(), self.rule('SCHEDULE_BLOCK', maximum=2)])
        self.assertEqual(result['score'], 30)
        self.assertEqual({x['period_type'] for x in result['violations']}, {'WEEK', 'MONTH', 'SCHEDULE_BLOCK'})

    def test_minimum_counts_empty_physician_month_and_rows_reconcile(self):
        result = self.report([date(2026, 12, 5), date(2027, 1, 2)],
                             [self.rule(minimum=1, maximum=None)], assigned={0})
        self.assertEqual(result['score'], 10)
        row = result['violations'][0]
        self.assertEqual(row['violation_type'], 'WEEKEND_UNDER_MINIMUM')
        self.assertEqual(row['period_start'], '2027-01-01')
        self.assertEqual(row['actual_value'], 0)
        self.assertEqual(row['shift_instance_ids'], [])
        self.assertEqual(sum(x['penalty'] for x in result['violations']), result['score'])

    def test_zero_weight_and_template_weekend_designation(self):
        dates = [date(2026, 12, 4), date(2026, 12, 5)]
        result = self.report(dates, [self.rule(maximum=0)], weekend_days=['Friday'])
        self.assertEqual(result['score'], 10)
        self.assertEqual(result['violations'][0]['shift_instance_ids'], [0])
        self.assertEqual(self.report(dates, [self.rule(maximum=0, weight=0)])['score'], 0)

    def test_fallback_balancing_is_unchanged(self):
        block = SimpleNamespace(start_date=date(2027, 1, 1), end_date=date(2027, 1, 31))
        instances = [SimpleNamespace(id=i, date=date(2027, 1, 2+i), schedule_block=block,
                                    shift_template=SimpleNamespace(weekend_days=['Saturday', 'Sunday', 'Monday', 'Tuesday'])) for i in range(4)]
        physicians = [SimpleNamespace(id=i) for i in (1, 2)]
        contracts = {i: SimpleNamespace(id=i, name='Default', weekend_settings={}) for i in (1, 2)}
        result = o._weekend_volume_report(instances, physicians, {i: [1] for i in range(4)}, contracts, details=True)
        self.assertEqual(result['score'], 100)
        self.assertEqual(result['violations'][0]['violation_type'], 'WEEKEND_CONCENTRATION')


class WeekendVolumeIntegrationTests(TestCase):
    def test_report_selected_delta_and_recalculation_preserve_assignments(self):
        version = Command()._build_fixture(SCALE_PROFILES[0], shifts_per_day=1)
        links = list(ContractUserAssignment.objects.filter(domain=version.domain).select_related('contract', 'physician__user'))
        contract = links[0].contract
        contract.weekend_settings = {'period_rules': [
            {'period_type': 'MONTH', 'min_volume': 1, 'max_volume': 2,
             'min_penalty_weight': 10, 'max_penalty_weight': 20},
            {'period_type': 'SCHEDULE_BLOCK', 'max_volume': 3, 'max_penalty_weight': 30},
        ]}
        contract.save()
        physicians = [a.physician for a in links]
        contracts = {p.id: contract for p in physicians}
        instances = list(version.shift_instances.select_related('shift_template', 'facility', 'schedule_block').order_by('date'))
        first, second = physicians[:2]
        state = defaultdict(list, {x.id: [first.id] for x in instances})
        targets = {p.id: o._version_contract_target(version, p.id, contract, Decimal(0), Decimal(0)) for p in physicians}
        facilities = {p.id: set(contract.facilities.values_list('id', flat=True)) for p in physicians}
        rest = {p.id: o._minimum_rest_hours(contract) for p in physicians}
        args = (targets, contracts, defaultdict(list), facilities, rest)
        before = o._score_schedule(instances, physicians, state, *args)
        trial = o._copy_state(state)
        shift = next(x for x in instances if x.date.strftime('%A') in x.shift_template.weekend_days)
        o._replace_in_state(trial, shift.id, first.id, second.id)
        after = o._score_schedule(instances, physicians, trial, *args)
        self.assertEqual(o._selected_physician_score_delta(instances, physicians, state, trial, {first.id, second.id}, *args), after['score']-before['score'])
        run = OptimizerRun.objects.create(schedule_version=version, run_number=1,
            status=OptimizerRun.Status.COMPLETED, is_active=True, initial_score=123, final_score=123)
        for instance in instances:
            ScheduleShiftAssignment.objects.create(shift_instance=instance, physician=first, optimizer_run=run,
                assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER)
        report = o.build_violation_report(version, run)
        rows = [x for u in report['users'] for x in u['violations'] if x['violation_type'].startswith('WEEKEND')]
        self.assertEqual(sum(x['penalty_amount'] for x in rows), float(before['breakdown']['weekend_score']))
        self.assertTrue(all('Weekend shifts are' in x['explanation'] for x in rows))
        self.assertTrue(all(x['period_start'] and x['period_end'] for x in rows))
        self.assertEqual(report['score_audit']['weekend_rows_total'], report['score_audit']['weekend_score'])
        self.assertFalse(report['score_audit']['warnings'])
        run.refresh_from_db()
        self.assertEqual(run.final_score, 123)
        self.assertEqual(run.assignments.count(), len(instances))
        saved_assignments = list(run.assignments.order_by('id').values_list('id', 'shift_instance_id', 'physician_id'))
        summary, _ = o.recalculate_schedule_version_score(version, run)
        expected = o._build_workload_summary(
            physicians, before['physician_hours'], before['physician_shifts'],
            before['physician_night_shifts'], targets, before['workload_score_rows'],
        )
        expected = {row['physician_id']: row for row in expected}
        for row in summary['workload_summary']:
            for key in ('effective_workload_range', 'raw_workload_rule', 'target', 'contract_id', 'score_contribution'):
                self.assertEqual(row[key], expected[row['physician_id']][key])
            self.assertIsNotNone(row['effective_workload_range'])
        self.assertEqual(summary['final_score'], report['total_score'])
        self.assertEqual(saved_assignments, list(run.assignments.order_by('id').values_list('id', 'shift_instance_id', 'physician_id')))
