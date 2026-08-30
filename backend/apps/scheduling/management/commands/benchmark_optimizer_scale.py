import json
from collections import Counter
from calendar import monthrange
from math import ceil
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Physician
from apps.domains.models import Domain
from apps.facilities.models import Facility
from apps.scheduling import optimizer
from apps.scheduling.run_state import assignments_for_viewed_run
from apps.scheduling.models import (
    Contract, ContractUserAssignment, ScheduleBlock, ScheduleShiftInstance,
    ScheduleVersion, ShiftTemplate, ScheduleShiftAssignment, ScheduleRequest,
)


SCALE_PROFILES = (
    {'name': '1m-5p', 'months': 1, 'physicians': 5},
    {'name': '2m-10p', 'months': 2, 'physicians': 10},
    {'name': '3m-25p', 'months': 3, 'physicians': 25},
    {'name': '6m-50p', 'months': 6, 'physicians': 50},
    {'name': '12m-100p', 'months': 12, 'physicians': 100},
    {'name': '12m-150p', 'months': 12, 'physicians': 150},
)


def _end_of_profile(start, months):
    month_index = start.month - 1 + months - 1
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, monthrange(year, month)[1])


class Command(BaseCommand):
    help = 'Run rollback-only optimizer scale profiles against isolated synthetic schedules.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--profile', action='append', choices=[row['name'] for row in SCALE_PROFILES],
            help='Run one or more named profiles. Omit to run the full scale matrix.',
        )
        parser.add_argument('--time-limit-seconds', type=float, default=10.0)
        parser.add_argument('--seed', type=int, default=73001)
        parser.add_argument('--shifts-per-day', type=int, default=2)
        parser.add_argument('--staffing-per-shift', type=int, default=1)
        parser.add_argument('--output-json')
        parser.add_argument('--fail-on-gate', action='store_true')
        parser.add_argument('--scenario', choices=('baseline', 'mixed'), default='baseline')

    def handle(self, *args, **options):
        if options['time_limit_seconds'] <= 0:
            raise CommandError('--time-limit-seconds must be greater than 0.')
        if options['shifts_per_day'] < 1 or options['shifts_per_day'] > 4:
            raise CommandError('--shifts-per-day must be between 1 and 4.')
        if options['staffing_per_shift'] < 1:
            raise CommandError('--staffing-per-shift must be at least 1.')
        if options['scenario'] == 'mixed' and options['shifts_per_day'] < 2:
            raise CommandError('Mixed constraints require at least two shifts per day, including a night.')
        names = set(options['profile'] or [])
        profiles = [row for row in SCALE_PROFILES if not names or row['name'] in names]
        results = []
        self.stdout.write('Safety: every synthetic profile runs inside a rolled-back transaction.')
        for index, profile in enumerate(profiles):
            with transaction.atomic():
                result = self._run_profile(
                    profile,
                    seed=options['seed'] + index,
                    time_limit=options['time_limit_seconds'],
                    shifts_per_day=options['shifts_per_day'],
                    staffing_per_shift=options['staffing_per_shift'],
                    scenario=options['scenario'],
                )
                transaction.set_rollback(True)
            results.append(result)
            self.stdout.write(
                f"{result['profile']}: {result['status']} {result['runtime_seconds']:.2f}s, "
                f"coverage={result['assigned_slots']}/{result['required_slots']}, "
                f"score={result['final_score']}, candidates={result['search_candidates']}, "
                f"full_scores={result['full_score_evaluations']}"
            )
            self.stdout.write(f"Quality gates: {result['quality_gates']}")
        report = {
            'benchmark_version': 3,
            'scenario': options['scenario'],
            'safety': 'All synthetic database rows and optimizer runs were rolled back.',
            'time_limit_seconds': options['time_limit_seconds'],
            'shifts_per_day': options['shifts_per_day'],
            'staffing_per_shift': options['staffing_per_shift'],
            'results': results,
        }
        if options['output_json']:
            path = Path(options['output_json'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        self.stdout.write('Scale matrix complete; no synthetic data was retained.')
        if options['fail_on_gate'] and any(
            not all(result['quality_gates'].values()) for result in results
        ):
            raise CommandError('One or more scale quality gates failed; synthetic data was rolled back.')

    def _run_profile(self, profile, *, seed, time_limit, shifts_per_day, staffing_per_shift, scenario='baseline'):
        version = self._build_fixture(
            profile, shifts_per_day=shifts_per_day, staffing_per_shift=staffing_per_shift,
            scenario=scenario,
        )
        fixed_pairs = set(ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version, is_locked=True,
        ).values_list('shift_instance_id', 'physician_id'))
        contract_fields = ('id', 'workload_settings', 'night_settings', 'shift_settings', 'request_settings', 'weekend_settings')
        contracts_before = list(Contract.objects.filter(domain=version.domain).values(*contract_fields))
        started = monotonic()
        with patch.object(optimizer, 'MAX_RUNTIME_SECONDS', time_limit):
            summary = optimizer.optimize_schedule_version(version, seed=seed)
        runtime = monotonic() - started
        debug = summary.get('debug', {})
        run = version.optimizer_runs.order_by('-run_number').first()
        required_slots = sum(
            version.shift_instances.values_list('required_staffing', flat=True)
        )
        persisted_pairs = list(assignments_for_viewed_run(version, run).values_list(
            'shift_instance_id', 'physician_id',
        ))
        counts = Counter(instance_id for instance_id, _ in persisted_pairs)
        coverage_complete = all(
            counts[instance_id] == required
            for instance_id, required in version.shift_instances.values_list('id', 'required_staffing')
        ) and len(persisted_pairs) == len(set(persisted_pairs))
        assigned_slots = len(persisted_pairs)
        initial_score = float(summary.get('initial_score', 0))
        final_score = float(summary.get('total_score', summary.get('final_score', 0)))
        candidates = int(debug.get('candidates_considered_before_timeout', 0))
        full_scores = int(debug.get('full_score_evaluations', 0))
        audit_started = monotonic()
        report = optimizer.build_violation_report(version, optimizer_run=run)
        audit_seconds = monotonic() - audit_started
        hard_fields = (
            'final_overlap_violations', 'final_rest_violations',
            'final_duplicate_violations', 'final_overstaffed_violations',
            'final_inactive_physician_violations', 'final_facility_ineligible_violations',
        )
        gates = {
            'completed': run is not None and run.status == 'COMPLETED',
            'persisted_coverage_complete': coverage_complete,
            'hard_constraints_valid': all(debug.get(field) == 0 for field in hard_fields),
            'persisted_score_matches': abs(report['total_score'] - final_score) < 0.01,
            'score_components_consistent': not report['score_audit']['warnings'],
            'never_worse': final_score <= initial_score,
            'fixed_assignments_preserved': fixed_pairs <= set(
                assignments_for_viewed_run(version, run).filter(is_locked=True).values_list(
                    'shift_instance_id', 'physician_id',
                )
            ),
            'contracts_unchanged': contracts_before == list(
                Contract.objects.filter(domain=version.domain).values(*contract_fields)
            ),
        }
        return {
            'profile': profile['name'],
            'scenario': scenario,
            'fixed_assignments': len(fixed_pairs),
            'requests': version.schedule_block.requests.count(),
            'months': profile['months'],
            'physicians': profile['physicians'],
            'block_days': (version.schedule_block.end_date - version.schedule_block.start_date).days + 1,
            'shift_instances': version.shift_instances.count(),
            'required_slots': required_slots,
            'assigned_slots': assigned_slots,
            'coverage_complete': coverage_complete,
            'partial_assignments_made': int(summary.get('assignments_made', 0)),
            'quality_gates': gates,
            'recomputed_score': report['total_score'],
            'score_breakdown': report['score_breakdown'],
            'audit_seconds': audit_seconds,
            'status': run.status if run else 'UNKNOWN',
            'timed_out': bool(debug.get('timed_out')),
            'initial_score': initial_score,
            'final_score': final_score,
            'never_worse': final_score <= initial_score,
            'runtime_seconds': runtime,
            'seed': seed,
            'search_candidates': candidates,
            'search_candidates_per_second': candidates / runtime if runtime else 0,
            'full_score_evaluations': full_scores,
            'full_scores_per_second': full_scores / runtime if runtime else 0,
        }

    def _build_fixture(self, profile, *, shifts_per_day, staffing_per_shift=1, scenario='baseline'):
        start = date(2032, 1, 1)
        end = _end_of_profile(start, profile['months'])
        suffix = profile['name']
        domain = Domain.objects.create(name=f'Scale {suffix}')
        facility = Facility.objects.create(
            name=f'Scale Facility {suffix}', short_name=f'Scale {suffix}', timezone='UTC',
        )
        block = ScheduleBlock.objects.create(
            start_date=start,
            end_date=end,
            request_open_datetime=timezone.make_aware(datetime(2031, 10, 1, 8)),
            request_close_datetime=timezone.make_aware(datetime(2031, 10, 31, 17)),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        version = ScheduleVersion.objects.create(
            schedule_block=block, domain=domain, version_number=1,
            name=f'Scale {suffix}', status=ScheduleVersion.Status.BUILD,
        )
        required_slots = ((end - start).days + 1) * shifts_per_day * staffing_per_shift
        target = required_slots / profile['physicians']
        contract = Contract.objects.create(
            domain=domain,
            name=f'Scale Contract {suffix}',
            workload_settings={'period_rules': [{
                'period_type': 'SCHEDULE_BLOCK', 'units': 'SHIFTS',
                'min_value': str(max(0, int(target) - 1)),
                'max_value': str(int(target) + 2),
                'min_penalty_weight': '100', 'max_penalty_weight': '100',
            }]},
            night_settings={'period_rules': [{
                'period_type': 'SCHEDULE_BLOCK', 'min_shifts': 0,
                'max_shifts': str(int(target) + 2),
                'min_penalty_weight': '100', 'max_penalty_weight': '100',
            }]},
        )
        contract.facilities.add(facility)
        users = User.objects.bulk_create([
            User(username=f'scale-{suffix}-{index}', first_name='Scale', last_name=f'{index:03d}')
            for index in range(profile['physicians'])
        ])
        physicians = Physician.objects.bulk_create([
            Physician(user=user, display_name=user.username, primary_facility=facility)
            for user in users
        ])
        ContractUserAssignment.objects.bulk_create([
            ContractUserAssignment(contract=contract, domain=domain, physician=physician)
            for physician in physicians
        ])
        starts = (time(7), time(19), time(11), time(15))
        ends = (time(15), time(7), time(19), time(23))
        templates = []
        for index in range(shifts_per_day):
            templates.append(ShiftTemplate.objects.create(
                facility=facility,
                start_time=starts[index], end_time=ends[index],
                active_days_of_week=list(ShiftTemplate.DAYS_OF_WEEK),
                weekend_days=['Friday', 'Saturday', 'Sunday'],
                night_shift=index == 1, default_staffing_count=staffing_per_shift,
            ))
        rows = []
        current = start
        while current <= end:
            for template in templates:
                starts_at = timezone.make_aware(datetime.combine(current, template.start_time))
                ending_date = current + timedelta(days=1) if template.end_time <= template.start_time else current
                rows.append(ScheduleShiftInstance(
                    schedule_version=version, schedule_block=block, date=current,
                    shift_template=template, facility=facility,
                    start_datetime=starts_at,
                    end_datetime=timezone.make_aware(datetime.combine(ending_date, template.end_time)),
                    required_staffing=staffing_per_shift,
                ))
            current += timedelta(days=1)
        ScheduleShiftInstance.objects.bulk_create(rows, batch_size=1000)
        if scenario == 'mixed':
            night_target = ((end - start).days + 1) * staffing_per_shift / profile['physicians']
            monthly_target = 31 * staffing_per_shift / profile['physicians']
            contract.night_settings = {'period_rules': [
                {'period_type': 'MONTH', 'min_shifts': int(monthly_target),
                 'max_shifts': ceil(monthly_target) + 1,
                 'min_penalty_weight': '1000', 'max_penalty_weight': '1000'},
                {'period_type': 'SCHEDULE_BLOCK', 'min_shifts': max(0, int(night_target) - 1),
                 'max_shifts': ceil(night_target) + 1,
                 'min_penalty_weight': '1000', 'max_penalty_weight': '1000'},
            ]}
            contract.save(update_fields=['night_settings'])
            fixed_nights = list(version.shift_instances.filter(
                shift_template__night_shift=True, date__day=1,
            ).order_by('date'))
            ScheduleShiftAssignment.objects.bulk_create([
                ScheduleShiftAssignment(
                    shift_instance=instance, physician=physicians[index % len(physicians)],
                    assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
                    is_locked=True,
                ) for index, instance in enumerate(fixed_nights)
            ])
            ScheduleRequest.objects.bulk_create([
                ScheduleRequest(
                    schedule_block=block, physician=physician,
                    date=start + timedelta(days=7 + index % 14),
                    request_scope=ScheduleRequest.RequestScope.USER,
                    request_type=ScheduleRequest.RequestType.DAY_OFF,
                    weight=ScheduleRequest.Weight.HIGH,
                ) for index, physician in enumerate(physicians)
            ])
        return version
