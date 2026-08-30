import json
from decimal import Decimal
from datetime import date, datetime, time
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Physician
from apps.domains.models import Domain
from apps.facilities.models import Facility

from .models import (
    Contract,
    ContractUserAssignment,
    OptimizerRun,
    ScheduleBlock,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    ShiftTemplate,
)
from .optimizer import optimize_schedule_version
from .workload_feasibility import _aggregate_hour_rule_bounds


class ExplainWorkloadFeasibilityCommandTests(TestCase):
    def test_overlapping_period_capacity_matches_42_full_and_two_fractional_users(self):
        def row(period, start, end, minimum, maximum):
            return {'period': period, 'period_start': start, 'period_end': end,
                    'effective_min_hours': minimum, 'effective_max_hours': maximum}
        full = [
            row('MONTH', '2026-12-01', '2026-12-31', 100, 140),
            row('MONTH', '2027-01-01', '2027-01-31', 100, 140),
            row('SCHEDULE_BLOCK', '2026-12-01', '2027-01-31', 200, 280),
        ]
        fractional = [
            row('MONTH', '2026-12-01', '2026-12-31', 60, 70),
            row('MONTH', '2027-01-01', '2027-01-31', 60, 70),
        ]
        full_min, full_max = _aggregate_hour_rule_bounds(full)
        part_min, part_max = _aggregate_hour_rule_bounds(fractional)
        self.assertEqual(full_min * 42 + part_min * 2, Decimal('8640'))
        self.assertEqual(full_max * 42 + part_max * 2, Decimal('12040'))
        self.assertEqual(_aggregate_hour_rule_bounds(full + full), (full_min, full_max))
        full[-1]['effective_min_hours'] = 220
        full[-1]['effective_max_hours'] = 260
        self.assertEqual(_aggregate_hour_rule_bounds(full), (Decimal('220'), Decimal('260')))
        full[-1]['effective_max_hours'] = None
        self.assertEqual(_aggregate_hour_rule_bounds(full), (Decimal('220'), Decimal('280')))

    def test_overlapping_rules_drive_correct_maximum_adjustment_preview(self):
        for contract in self.contracts:
            contract.workload_settings = {'period_rules': [
                {'period_type': period, 'units': 'HOURS', 'min_value': '0', 'max_value': '4'}
                for period in ('SCHEDULE_BLOCK', 'WEEK')
            ]}
            contract.save(update_fields=['workload_settings'])
        report = self._run()['aggregate_feasibility']
        self.assertEqual(report['sum_effective_maximum_hours'], 8)
        self.assertEqual(report['status'], 'maximum_infeasible')
        self.assertEqual(report['fte_adjustment_preview']['direction'], 'increase_maximum')
        self.assertEqual(report['fte_adjustment_preview']['required_adjustment_hours'], 2)

    def setUp(self):
        self.domain = Domain.objects.create(name='Physician', active=True)
        self.facility = Facility.objects.create(name='Hours Hospital', short_name='Hours')
        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1),
            request_open_datetime=timezone.make_aware(datetime(2026, 6, 1, 8)),
            request_close_datetime=timezone.make_aware(datetime(2026, 6, 2, 8)),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        self.version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Workload fixture',
            status=ScheduleVersion.Status.BUILD,
        )
        template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7),
            end_time=time(17),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            default_staffing_count=1,
        )
        self.instance = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=self.version,
            shift_template=template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 1, 17)),
            required_staffing=1,
        )
        self.physicians = []
        self.contracts = []
        for index, last_name in enumerate(('Turner', 'Full')):
            user = get_user_model().objects.create_user(
                username=f'workload{index}@example.com',
                first_name='Workload',
                last_name=last_name,
            )
            physician = Physician.objects.create(
                user=user,
                display_name=f'Workload {last_name}',
                fte='0.50' if last_name == 'Turner' else '1.00',
            )
            contract = Contract.objects.create(
                domain=self.domain,
                name=f'{last_name} Contract',
                active=True,
            )
            contract.facilities.add(self.facility)
            ContractUserAssignment.objects.create(
                contract=contract,
                domain=self.domain,
                physician=physician,
            )
            self.physicians.append(physician)
            self.contracts.append(contract)

    def _set_ranges(self, minimum, maximum):
        for contract in self.contracts:
            contract.workload_settings = {
                'period_rules': [{
                    'period_type': 'SCHEDULE_BLOCK',
                    'units': 'HOURS',
                    'min_value': str(minimum),
                    'max_value': str(maximum),
                    'min_penalty_weight': '100',
                    'max_penalty_weight': '100',
                }],
            }
            contract.save(update_fields=['workload_settings', 'updated_at'])

    def _run(self, optimizer_run=None):
        stdout = StringIO()
        options = {
            'schedule_block_id': self.block.id,
            'domain': self.domain.name,
            'stdout': stdout,
        }
        if optimizer_run is not None:
            options['optimizer_run_id'] = optimizer_run.id
        call_command('explain_workload_feasibility', **options)
        return json.loads(stdout.getvalue())

    def test_aggregate_minimum_infeasible(self):
        self._set_ranges(6, 20)

        report = self._run()

        aggregate = report['aggregate_feasibility']
        self.assertEqual(aggregate['sum_effective_minimum_hours'], 12.0)
        self.assertEqual(aggregate['status'], 'minimum_infeasible')
        self.assertIn('collectively infeasible', aggregate['interpretation'])
        preview = aggregate['fte_adjustment_preview']
        self.assertEqual(preview['direction'], 'decrease_minimum')
        self.assertEqual(preview['required_adjustment_hours'], 2.0)
        self.assertEqual(preview['total_applicable_fte'], 1.5)
        proposals = {row['physician']: row for row in preview['proposals']}
        self.assertAlmostEqual(proposals['Workload Turner']['adjustment_hours'], 2 / 3)
        self.assertAlmostEqual(proposals['Workload Full']['adjustment_hours'], 4 / 3)
        self.assertAlmostEqual(
            sum(row['proposed_hours'] for row in preview['proposals']),
            10.0,
        )

    def test_aggregate_maximum_infeasible(self):
        self._set_ranges(0, 4)

        report = self._run()

        aggregate = report['aggregate_feasibility']
        self.assertEqual(aggregate['sum_effective_maximum_hours'], 8.0)
        self.assertEqual(aggregate['status'], 'maximum_infeasible')
        preview = aggregate['fte_adjustment_preview']
        self.assertEqual(preview['direction'], 'increase_maximum')
        self.assertEqual(preview['required_adjustment_hours'], 2.0)
        self.assertAlmostEqual(preview['adjustment_hours_per_fte'], 4 / 3)
        proposals = {row['physician']: row for row in preview['proposals']}
        self.assertAlmostEqual(proposals['Workload Turner']['adjustment_hours'], 2 / 3)
        self.assertAlmostEqual(proposals['Workload Full']['adjustment_hours'], 4 / 3)
        self.assertAlmostEqual(
            sum(row['proposed_hours'] for row in preview['proposals']),
            10.0,
        )

    def test_aggregate_feasible(self):
        self._set_ranges(4, 6)

        report = self._run()

        aggregate = report['aggregate_feasibility']
        self.assertEqual(aggregate['status'], 'aggregate_feasible')
        self.assertIsNone(aggregate['fte_adjustment_preview'])
        self.assertEqual(aggregate['available_minus_total_minimum'], 2.0)
        self.assertEqual(aggregate['total_maximum_minus_available'], 2.0)
        self.assertEqual(report['reduced_contract_focus'][0]['physician'], 'Workload Turner')

    def test_optimizer_run_assigned_hours_and_score_contributions_are_included(self):
        self._set_ranges(4, 6)
        optimizer_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='BENCHMARK',
            final_score=400,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[0],
            optimizer_run=optimizer_run,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )

        report = self._run(optimizer_run)

        summary = report['schedule_block']
        self.assertEqual(summary['total_generated_required_hours'], 10.0)
        self.assertEqual(summary['total_run_assigned_hours'], 10.0)
        self.assertEqual(summary['total_visible_assigned_hours'], 10.0)
        self.assertEqual(summary['total_unfilled_hours'], 0.0)
        self.assertEqual(summary['assignment_count'], 1)
        self.assertEqual(summary['duplicate_shift_assignment_count'], 0)
        rows = {row['physician']: row for row in report['physicians']}
        self.assertEqual(rows['Workload Turner']['assigned_hours'], 10.0)
        self.assertEqual(rows['Workload Turner']['surplus_hours'], 4.0)
        self.assertEqual(rows['Workload Turner']['workload_score_contribution'], 400.0)
        self.assertEqual(rows['Workload Full']['assigned_hours'], 0.0)
        self.assertEqual(rows['Workload Full']['deficit_hours'], 4.0)
        self.assertEqual(rows['Workload Full']['workload_score_contribution'], 400.0)

    def test_ordinary_run_reports_legacy_manual_layer_and_duplicate_pair(self):
        self._set_ranges(0, 20)
        optimizer_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='OPTIMIZER',
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[0],
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[0],
            optimizer_run=optimizer_run,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )

        report = self._run(optimizer_run)

        accounting = report['assignment_accounting']
        self.assertEqual(accounting['total_run_assigned_hours'], 10.0)
        self.assertEqual(accounting['total_visible_assigned_hours'], 20.0)
        self.assertEqual(accounting['total_unfilled_hours'], 0.0)
        self.assertEqual(accounting['assignment_count'], 2)
        self.assertEqual(accounting['duplicate_shift_assignment_count'], 1)
        self.assertEqual(accounting['assignments_above_required_staffing_count'], 1)
        self.assertEqual(accounting['assignments_above_required_staffing_hours'], 10.0)
        self.assertEqual(accounting['legacy_unscoped_manual_assignment_count'], 1)
        self.assertEqual(accounting['visible_assignments_outside_selected_run_count'], 1)
        self.assertEqual(accounting['selected_run_duplicate_assignment_count'], 0)
        duplicate = accounting['duplicate_assignment_list'][0]
        self.assertEqual(duplicate['shift_instance_id'], self.instance.id)
        self.assertEqual(duplicate['physician_id'], self.physicians[0].id)
        self.assertEqual(
            {row['optimizer_run_id'] for row in duplicate['assignments']},
            {None, optimizer_run.id},
        )
        rows = {row['physician']: row for row in report['physicians']}
        self.assertEqual(rows['Workload Turner']['assigned_hours'], 10.0)

    def test_required_staffing_allows_different_physicians_without_duplicates(self):
        self._set_ranges(0, 20)
        self.instance.required_staffing = 2
        self.instance.save(update_fields=['required_staffing', 'updated_at'])
        optimizer_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='BENCHMARK',
        )
        for physician in self.physicians:
            ScheduleShiftAssignment.objects.create(
                shift_instance=self.instance,
                physician=physician,
                optimizer_run=optimizer_run,
                assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            )

        report = self._run(optimizer_run)

        accounting = report['assignment_accounting']
        self.assertEqual(accounting['assignment_count'], 2)
        self.assertEqual(accounting['duplicate_shift_assignment_count'], 0)
        self.assertEqual(accounting['assignments_above_required_staffing_count'], 0)
        valid = accounting['extra_assignment_categories']['valid_multi_staff_shift_assignments']
        self.assertEqual(valid[0]['required_staffing'], 2)
        self.assertEqual(valid[0]['visible_assignment_count'], 2)

    def test_optimizer_normalizes_duplicate_and_over_capacity_source_snapshot(self):
        self._set_ranges(0, 20)
        source = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='OPTIMIZER',
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[0],
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
        )
        for physician in self.physicians:
            ScheduleShiftAssignment.objects.create(
                shift_instance=self.instance,
                physician=physician,
                optimizer_run=source,
                assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            )

        summary = optimize_schedule_version(
            self.version,
            seed=17,
            start_mode=OptimizerRun.StartMode.CURRENT_SCHEDULE,
            source_run=source,
        )

        created = OptimizerRun.objects.get(id=summary['optimizer_run_id'])
        pairs = list(
            ScheduleShiftAssignment.objects.filter(optimizer_run=created)
            .values_list('shift_instance_id', 'physician_id')
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertLessEqual(len(pairs), self.instance.required_staffing)
        self.assertEqual(
            created.optimizer_debug['source_assignment_normalization']['duplicate_rows_discarded'],
            1,
        )
        self.assertEqual(
            created.optimizer_debug['source_assignment_normalization']['excess_rows_discarded'],
            1,
        )

    def test_under_minimum_physician_lists_individual_transfer_candidate(self):
        self._set_ranges(4, 20)
        optimizer_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='BENCHMARK',
        )
        assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[1],
            optimizer_run=optimizer_run,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )

        report = self._run(optimizer_run)

        turner = next(
            row for row in report['under_minimum_physicians']
            if row['physician'] == 'Workload Turner'
        )
        diagnostic = turner['under_minimum_diagnostic']
        self.assertEqual(turner['assigned_hours'], 0.0)
        self.assertEqual(turner['deficit_hours'], 4.0)
        self.assertEqual(diagnostic['assessment'], 'allocation-related')
        self.assertEqual(diagnostic['legal_candidate_count'], 1)
        candidate = diagnostic['legal_candidate_shifts'][0]
        self.assertEqual(candidate['action'], 'replace_unlocked_assignment')
        self.assertEqual(candidate['replace_assignment_id'], assignment.id)
        self.assertLessEqual(len(diagnostic['legal_candidate_shifts']), 20)
        self.assertEqual(
            set(diagnostic['rejected_candidate_counts_by_reason']),
            {
                'overlap', 'rest', 'request_conflict', 'night_maximum',
                'facility_ineligible', 'would_exceed_max_workload',
                'locked_assignment', 'already_assigned',
            },
        )

    def test_under_minimum_physician_reports_locked_constraint(self):
        self._set_ranges(4, 20)
        optimizer_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            run_kind='BENCHMARK',
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.instance,
            physician=self.physicians[1],
            optimizer_run=optimizer_run,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            is_locked=True,
        )

        report = self._run(optimizer_run)

        turner = next(
            row for row in report['under_minimum_physicians']
            if row['physician'] == 'Workload Turner'
        )
        diagnostic = turner['under_minimum_diagnostic']
        self.assertEqual(diagnostic['assessment'], 'constraint-blocked')
        self.assertEqual(diagnostic['legal_candidate_count'], 0)
        self.assertEqual(
            diagnostic['rejected_candidate_counts_by_reason']['locked_assignment'], 1
        )
