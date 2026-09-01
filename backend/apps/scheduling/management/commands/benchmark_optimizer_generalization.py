"""Paired, rollback-only screening; never use a customer's schedule as a fixture."""
import json
from collections import defaultdict
from contextlib import ExitStack
from math import ceil
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from .benchmark_optimizer_scale import Command as ScaleCommand, SCALE_PROFILES
from apps.scheduling import optimizer
from apps.scheduling.models import Contract, ContractUserAssignment, OptimizerRun, ScheduleShiftAssignment
from apps.scheduling.search_budget import SearchBudget


CASES = (
    ('1m-5p', 'fresh', 'baseline'),
    ('2m-10p', 'partial', 'mixed'),
    ('3m-25p', 'uneven', 'mixed'),
    ('6m-50p', 'balanced', 'mixed'),
    ('12m-100p', 'fresh', 'mixed'),
    ('12m-150p', 'partial', 'conflicting'),
)
VARIANTS = ('current', 'without_targeted_repairs', 'without_general_repair', 'without_general_swaps')


class ScreeningRunner(ScaleCommand):
    def __init__(self, *, starting_point, scenario, **kwargs):
        super().__init__(**kwargs)
        self.starting_point = starting_point
        self.screening_scenario = scenario
        self.source = None
        self.starting_slots = 0

    def _build_fixture(self, profile, *, shifts_per_day, staffing_per_shift, scenario):
        version = super()._build_fixture(profile, shifts_per_day=shifts_per_day,
            staffing_per_shift=staffing_per_shift, scenario='baseline' if scenario == 'baseline' else 'mixed')
        contract = Contract.objects.get(domain=version.domain)
        if scenario != 'baseline':
            daily = shifts_per_day * staffing_per_shift / profile['physicians']
            contract.workload_settings['period_rules'].extend([
                {'period_type': 'WEEK', 'units': 'SHIFTS', 'min_value': '0',
                 'max_value': str(ceil(daily * 7) + 1), 'min_penalty_weight': '100', 'max_penalty_weight': '100'},
                {'period_type': 'MONTH', 'units': 'SHIFTS', 'min_value': str(max(0, int(daily * 28) - 1)),
                 'max_value': str(ceil(daily * 31) + 2), 'min_penalty_weight': '100', 'max_penalty_weight': '100'},
            ])
            contract.night_settings['days_off_after_night_block'] = 2
            contract.night_settings['days_off_before_next_night_shift'] = 3
            contract.night_settings['period_rules'].append({
                'period_type': 'WEEK', 'min_shifts': 0, 'max_shifts': 3,
                'min_penalty_weight': '1000', 'max_penalty_weight': '1000'})
            if scenario == 'conflicting':
                for rule in contract.night_settings['period_rules']:
                    if rule['period_type'] == 'SCHEDULE_BLOCK':
                        rule['max_shifts'] = 1
            contract.save(update_fields=['workload_settings', 'night_settings'])
        if self.starting_point != 'fresh':
            self.source = self._seed_start(version, contract)
        self.starting_slots = ScheduleShiftAssignment.objects.filter(shift_instance__schedule_version=version).count()
        return version

    def _seed_start(self, version, contract):
        source = OptimizerRun.objects.create(schedule_version=version, run_number=1,
            status=OptimizerRun.Status.COMPLETED, is_active=True)
        physicians = list(ContractUserAssignment.objects.filter(domain=version.domain)
                          .order_by('physician_id').values_list('physician_id', flat=True))
        instances = list(version.shift_instances.select_related('shift_template').order_by('start_datetime', 'id'))
        by_id = {x.id: x for x in instances}
        state = defaultdict(list)
        indices = {pid: optimizer._AssignmentIntervalIndex() for pid in physicians}
        for sid, pid in ScheduleShiftAssignment.objects.filter(shift_instance__schedule_version=version).values_list('shift_instance_id', 'physician_id'):
            state[sid].append(pid)
            indices[pid].append((by_id[sid].start_datetime, by_id[sid].end_datetime))
        rows = []
        rest = optimizer._minimum_rest_hours(contract)
        for index, instance in enumerate(instances):
            if self.starting_point == 'partial' and index % 3 == 0:
                continue
            offset = 0 if self.starting_point == 'uneven' else index % len(physicians)
            order = physicians[offset:] + physicians[:offset]
            for pid in order:
                if len(state[instance.id]) >= instance.required_staffing:
                    break
                if pid in state[instance.id] or indices[pid].conflicts(instance, rest):
                    continue
                state[instance.id].append(pid)
                indices[pid].append((instance.start_datetime, instance.end_datetime))
                rows.append(ScheduleShiftAssignment(shift_instance=instance, physician_id=pid,
                    optimizer_run=source, assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER))
        ScheduleShiftAssignment.objects.bulk_create(rows, batch_size=1000)
        score = optimizer.build_violation_report(version, source)['total_score']
        source.initial_score = source.final_score = score
        source.save(update_fields=['initial_score', 'final_score'])
        return source


def disabled_repair(**kwargs):
    scoring = optimizer._score_schedule(kwargs['instances'], kwargs['physicians'], kwargs['state'],
        kwargs['targets'], kwargs['contract_by_physician'], kwargs['requests_by_physician_date'],
        kwargs['eligible_facilities_by_physician'], kwargs['minimum_rest_by_physician'])
    return kwargs['state'], scoring, {'attempts': 0, 'accepts': [], 'stopped_reason': 'disabled_for_comparison'}


def run_case(case, *, seed, variant, seconds):
    name, starting_point, scenario = case
    profile = next(p for p in SCALE_PROFILES if p['name'] == name)
    staffing = max(1, ceil(profile['physicians'] / 8))
    runner = ScreeningRunner(starting_point=starting_point, scenario=scenario)
    real_optimize = optimizer.optimize_schedule_version
    adaptive = {}
    phase_timings = {}
    def optimize(version, **kwargs):
        result = real_optimize(version, **kwargs, adaptive_runtime=True,
            start_mode='FRESH_FILL' if starting_point == 'fresh' else 'CURRENT_SCHEDULE',
            source_run=runner.source, run_kind='BENCHMARK')
        adaptive.update(result.get('debug', {}).get('adaptive_runtime', {}))
        phase_timings.update(result.get('debug', {}).get('phase_timings_seconds', {}))
        return result
    def budget(**kwargs):
        return SearchBudget(**kwargs, stall_seconds=seconds, total_seconds=seconds)
    started = monotonic()
    with transaction.atomic(), ExitStack() as stack:
        stack.enter_context(patch.object(optimizer, 'optimize_schedule_version', side_effect=optimize))
        stack.enter_context(patch.object(optimizer, 'SearchBudget', side_effect=budget))
        if variant == 'without_targeted_repairs':
            for name in ('_repair_workload_transfers', '_repair_night_spacing_swaps', '_repair_recovery_day_swaps'):
                stack.enter_context(patch.object(optimizer, name, side_effect=disabled_repair))
        elif variant == 'without_general_repair':
            for name in ('_repair_general_constraint_reassignments', '_repair_general_constraint_swaps'):
                stack.enter_context(patch.object(optimizer, name, side_effect=disabled_repair))
        elif variant == 'without_general_swaps':
            stack.enter_context(patch.object(
                optimizer, '_repair_general_constraint_swaps', side_effect=disabled_repair,
            ))
        # Half the short screening budget for the original pass, then continuation.
        result = runner._run_profile(profile, seed=seed, time_limit=seconds / 2,
            shifts_per_day=2, staffing_per_shift=staffing, scenario=scenario)
        transaction.set_rollback(True)
    result.update(starting_point=starting_point, variant=variant, adaptive=adaptive,
                  phase_timings_seconds=phase_timings,
                  starting_slots=runner.starting_slots, wall_seconds=monotonic() - started,
                  screening_budget_seconds=seconds, staffing_per_shift=staffing,
                  expected_soft_conflict=scenario == 'conflicting')
    # Coverage-first completion can properly increase the score of an incomplete source.
    result['quality_gates']['coverage_first_nonregression'] = (
        result['never_worse'] or (result['starting_slots'] < result['required_slots'] and result['coverage_complete']))
    return result


class Command(BaseCommand):
    help = 'Bounded generalization screening with paired seeds and rollback-only fixtures.'

    def add_arguments(self, parser):
        parser.add_argument('--seconds', type=float, default=5)
        parser.add_argument('--seeds', type=int, nargs='+', default=[81001, 81002])
        parser.add_argument('--profile', action='append', choices=[case[0] for case in CASES])
        parser.add_argument('--variant', action='append', choices=VARIANTS)
        parser.add_argument('--matrix-budget-seconds', type=float, default=300)
        parser.add_argument('--output-json', required=True)

    def handle(self, *args, **options):
        if options['seconds'] <= 0 or options['matrix_budget_seconds'] <= 0:
            raise CommandError('Time budgets must be positive.')
        cases = [c for c in CASES if not options['profile'] or c[0] in options['profile']]
        variants = options['variant'] or VARIANTS
        jobs = [(case, seed, variant) for case in cases for seed in options['seeds'] for variant in variants]
        report = {'scope': 'Synthetic screening, not a production scalability certification.',
                  'safety': 'All fixture and optimizer writes rolled back; sequence IDs may advance.',
                  'limitations': ['Short search budgets; setup, persistence and audit timed separately.',
                                 'One facility; no eligibility fragmentation or FTE heterogeneity in this screening.',
                                 'Profile and starting-point effects are confounded; this is not a full factorial matrix.',
                                 'Current adaptive loop emphasizes targeted repairs; ablation is not a replacement optimizer.'],
                  'planned_cases': len(jobs), 'results': []}
        started = monotonic()
        for case, seed, variant in jobs:
            if monotonic() - started >= options['matrix_budget_seconds']:
                break
            self.stdout.write(f'Starting {case[0]} / {case[1]} / {variant} / seed {seed}')
            result = run_case(case, seed=seed, variant=variant, seconds=options['seconds'])
            report['results'].append(result)
            report['completed_cases'] = len(report['results'])
            report['wall_seconds'] = monotonic() - started
            # Output is an explicitly requested benchmark artifact, never fixture data.
            path = Path(options['output_json'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2), encoding='utf-8')
            self.stdout.write(f"  score {result['initial_score']} -> {result['final_score']}; coverage "
                              f"{result['assigned_slots']}/{result['required_slots']}; wall {result['wall_seconds']:.1f}s")
        self.stdout.write(f"Completed {len(report['results'])}/{len(jobs)} cases. All synthetic data rolled back.")
