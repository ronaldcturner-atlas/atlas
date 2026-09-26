from collections import Counter, OrderedDict, defaultdict, deque
from bisect import bisect_left, bisect_right
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from datetime import timedelta
from itertools import islice
from heapq import nsmallest
from functools import lru_cache
import random
import secrets
from time import monotonic
from ortools.sat.python import cp_model
from .search_budget import SearchBudget

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import (
    ContractUserAssignment,
    OptimizerRun,
    ScheduleBlock,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
)
from .run_state import assignments_for_viewed_run


_FULL_SCORE_EVALUATIONS = ContextVar('optimizer_full_score_evaluations', default=0)
_SCORE_CACHE = ContextVar('optimizer_score_cache', default=None)
_SCORE_CACHE_HITS = ContextVar('optimizer_score_cache_hits', default=0)
_SCORE_CACHE_MISSES = ContextVar('optimizer_score_cache_misses', default=0)
_SCORE_CACHE_EVICTIONS = ContextVar('optimizer_score_cache_evictions', default=0)


COVERAGE_PENALTY = 1000
REST_VIOLATION_PENALTY = 10000
OVERLAP_VIOLATION_PENALTY = 10000
FACILITY_ELIGIBILITY_PENALTY = 10000
INACTIVE_PHYSICIAN_PENALTY = 10000
DEFAULT_MIN_REST_HOURS = Decimal('10')
DEFAULT_MAX_CONSECUTIVE_DAYS = 5
DEFAULT_CONSECUTIVE_DAYS_PENALTY = Decimal('500')
DEFAULT_MAX_SAME_SHIFT_STREAK = 2
DEFAULT_SAME_SHIFT_PENALTY = Decimal('2000')
DEFAULT_MAX_CONSECUTIVE_NIGHTS = 4
DEFAULT_CONSECUTIVE_NIGHTS_PENALTY = Decimal('500')
DEFAULT_NIGHT_BALANCE_PENALTY = Decimal('100')
DEFAULT_NIGHT_BLOCK_EXTENSION_BONUS = Decimal('2000')
DEFAULT_NIGHT_MINIMUM_PRIORITY_BONUS = Decimal('50000')
RECOVERY_CONFLICT_AVOIDANCE_MULTIPLIER = Decimal('3')
DEFAULT_WORKLOAD_RULE_PENALTY = Decimal('100')
DEFAULT_WEEKEND_BALANCE_PENALTY = Decimal('100')
DEFAULT_FACILITY_CONCENTRATION_PENALTY = Decimal('50')
ZERO_SHIFT_UNDERUTILIZATION_PENALTY = Decimal('5000')
DEFAULT_REQUEST_WEIGHTS = {
    ScheduleRequest.Weight.LOW: 10,
    ScheduleRequest.Weight.MEDIUM: 30,
    ScheduleRequest.Weight.HIGH: 90,
    ScheduleRequest.Weight.FIXED: 250,
}
MAX_PHASE_PASSES = 25
MAX_CANDIDATES_PER_REPAIR = 250
MAX_GENERAL_SWAPS = 25000
SAFE_BASELINE_PHASE_PASSES = 1
SAFE_BASELINE_CANDIDATES_PER_REPAIR = 40
SAFE_BASELINE_GENERAL_SWAPS = 50
MAX_RUNTIME_SECONDS = 900
ZERO_GAIN_PIPELINE_EPOCHS_BEFORE_DEEP_RESTART = 2
NIGHT_BLOCK_BUILDER_ENABLED = False
NIGHT_BLOCK_BUILDER_DISABLED_REASON = 'Disabled after runtime regression'
# Keep the inexpensive structural guidance even while the exhaustive block
# constructor is disabled.  A fresh fill otherwise treats hundreds of night
# slots as unrelated assignments and leaves local search with a prohibitively
# large number of broken night blocks to repair.
NIGHT_CONSTRUCTION_HEURISTICS_ENABLED = True
SCORE_CACHE_MAX_ENTRIES = 128


def _score_cache_key(state, include_internal_night_heuristics=False):
    """Encode an assignment state exactly and independently of dict/list order.

    The cache is scoped to one optimizer invocation, where instances, rules,
    contracts, requests, and eligibility are immutable. Keeping the complete
    encoded state in the key avoids relying on a hash collision assumption.
    """
    encoded = bytearray()
    for instance_id, physician_ids in sorted(state.items()):
        encoded.extend(int(instance_id).to_bytes(8, 'big', signed=False))
        normalized_physician_ids = sorted(int(value) for value in physician_ids)
        encoded.extend(len(normalized_physician_ids).to_bytes(4, 'big', signed=False))
        for physician_id in normalized_physician_ids:
            encoded.extend(physician_id.to_bytes(8, 'big', signed=False))
    return bool(include_internal_night_heuristics), bytes(encoded)


def _next_exhausted_pipeline_epoch_count(current_count, *, productive):
    """Reset exhaustion evidence on progress; otherwise add one full epoch."""
    return 0 if productive else int(current_count) + 1


def _pipeline_epoch_transition(current_count, *, productive, epoch_kind):
    """Choose a soft restart, deep restart, or stop from retained progress.

    Ordinary seed epochs preserve useful portfolio learning. Two consecutive
    zero-gain epochs justify a deeper reset of seed-local strategy evidence.
    If that genuinely fresh portfolio also cannot improve the global best,
    the search is productively exhausted.
    """
    exhausted_count = _next_exhausted_pipeline_epoch_count(
        current_count, productive=productive,
    )
    if productive:
        return exhausted_count, 'soft_restart'
    if epoch_kind == 'deep':
        return exhausted_count, 'stop'
    if exhausted_count >= ZERO_GAIN_PIPELINE_EPOCHS_BEFORE_DEEP_RESTART:
        return exhausted_count, 'deep_restart'
    return exhausted_count, 'soft_restart'


def _reset_adaptive_repair_epoch_state(repair_stats, cycle):
    """Reset epoch-local ROI without forgetting repeatedly failed tactics.

    A new seed changes the schedule being explored, so every repair should get
    another chance. It does not make an expensive repair that repeatedly
    produced no candidates entirely unknown again. Retaining consecutive
    failure evidence lets the new epoch probe it briefly and then restore the
    appropriate backoff instead of relearning the same fact for a full slice.
    """
    for stats in repair_stats.values():
        stats['epoch_calls'] = 0
        stats['recent_runtime_seconds'] = 0.0
        stats['recent_score_improvement'] = 0.0
        stats['cooldown_until_cycle'] = cycle
        stats['deep_epoch_resets'] = int(stats.get('deep_epoch_resets', 0)) + 1


def _physician_display_name(physician):
    return physician.display_name or physician.user.get_full_name() or physician.user.username


def _decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _positive_decimal_or_default(value, default):
    parsed = _decimal_or_none(value)
    if parsed is None or parsed < 0:
        return Decimal(str(default))
    return parsed


def _positive_int_or_default(value, default):
    parsed = _decimal_or_none(value)
    if parsed is None or parsed <= 0:
        return default
    return max(int(parsed), 1)


def _shift_hours(instance):
    seconds = (instance.end_datetime - instance.start_datetime).total_seconds()
    return max(Decimal(str(seconds)) / Decimal('3600'), Decimal('0'))


def _overlaps(instance, assigned_intervals):
    return any(
        start < instance.end_datetime and end > instance.start_datetime
        for start, end in assigned_intervals
    )


def _minimum_rest_hours(contract):
    settings = contract.workload_settings if isinstance(contract.workload_settings, dict) else {}
    configured = _decimal_or_none(settings.get('min_time_off_hours'))
    if configured is None or configured < 0:
        return DEFAULT_MIN_REST_HOURS
    return configured


def _rest_violation(instance, assigned_intervals, minimum_rest_hours):
    minimum_rest = timedelta(hours=float(minimum_rest_hours))
    for start, end in assigned_intervals:
        if end <= instance.start_datetime:
            if instance.start_datetime - end < minimum_rest:
                return True
            continue
        if instance.end_datetime <= start:
            if start - instance.end_datetime < minimum_rest:
                return True
    return False


class _AssignmentIntervalIndex:
    """Exact overlap/rest queries, including overlapping fixed assignments."""

    def __init__(self):
        self.starts = []
        self.ends = []
        self.prefix_max_ends = []
        self.has_invalid_interval = False

    def append(self, interval):
        start, end = interval
        self.has_invalid_interval |= end <= start
        position = bisect_right(self.starts, start)
        self.starts.insert(position, start)
        self.ends.insert(position, end)
        self.prefix_max_ends.insert(position, end)
        for index in range(position, len(self.ends)):
            self.prefix_max_ends[index] = (
                max(self.prefix_max_ends[index - 1], self.ends[index])
                if index else self.ends[index]
            )

    def conflicts(self, instance, minimum_rest_hours):
        if self.has_invalid_interval or instance.end_datetime <= instance.start_datetime:
            intervals = list(zip(self.starts, self.ends))
            return _overlaps(instance, intervals) or _rest_violation(
                instance, intervals, minimum_rest_hours,
            )
        rest = max(timedelta(0), timedelta(hours=float(minimum_rest_hours)))
        position = bisect_left(self.starts, instance.end_datetime + rest)
        return bool(
            position and self.prefix_max_ends[position - 1] > instance.start_datetime - rest
        )


def _contract_target(contract, default_hours_target, default_shift_target):
    settings = contract.workload_settings if isinstance(contract.workload_settings, dict) else {}
    rules = []
    for rule in settings.get('period_rules') or []:
        if not isinstance(rule, dict):
            continue
        min_value = _decimal_or_none(rule.get('min_value'))
        max_value = _decimal_or_none(rule.get('max_value'))
        values = [value for value in [min_value, max_value] if value is not None]
        if not values:
            continue
        units = 'SHIFTS' if rule.get('units') == 'SHIFTS' else 'HOURS'
        rules.append(
            {
                'period_type': rule.get('period_type') or 'SCHEDULE_BLOCK',
                'units': units,
                'min_value': min_value,
                'max_value': max_value,
                'min_penalty_weight': _positive_decimal_or_default(
                    rule.get('min_penalty_weight'),
                    Decimal('0'),
                ),
                'max_penalty_weight': _positive_decimal_or_default(
                    rule.get('max_penalty_weight'),
                    Decimal('0'),
                ),
            }
        )

    if rules:
        primary = rules[0]
        values = [
            value
            for value in [primary['min_value'], primary['max_value']]
            if value is not None
        ]
        target = sum(values) / Decimal(len(values))
        return {
            'units': primary['units'],
            'target': target,
            'rules': rules,
            'contract_id': contract.id,
            'contract_name': contract.name,
        }
    return {
        'units': 'HOURS',
        'target': default_hours_target or default_shift_target,
        'rules': [],
        'contract_id': contract.id,
        'contract_name': contract.name,
    }


def _with_workload_hour_override(target, override):
    """Replace hour rules with one build-only Schedule Block range."""
    if not isinstance(override, dict):
        return target
    minimum = _decimal_or_none(override.get('minimum_hours'))
    maximum = _decimal_or_none(override.get('maximum_hours'))
    if minimum is None and maximum is None:
        return target
    hour_rules = [rule for rule in target.get('rules') or [] if rule['units'] == 'HOURS']
    template = hour_rules[0] if hour_rules else {}
    rule = {
        'period_type': 'SCHEDULE_BLOCK',
        'units': 'HOURS',
        'min_value': minimum,
        'max_value': maximum,
        'min_penalty_weight': template.get('min_penalty_weight', Decimal('0')),
        'max_penalty_weight': template.get('max_penalty_weight', Decimal('0')),
    }
    rules = [rule for rule in target.get('rules') or [] if rule['units'] != 'HOURS'] + [rule]
    values = [value for value in (minimum, maximum) if value is not None]
    return {**target, 'units': 'HOURS', 'target': sum(values) / Decimal(len(values)), 'rules': rules}


def _version_contract_target(version, physician_id, contract, default_hours_target, default_shift_target):
    target = _contract_target(contract, default_hours_target, default_shift_target)
    overrides = version.workload_hour_overrides if isinstance(version.workload_hour_overrides, dict) else {}
    return _with_workload_hour_override(target, overrides.get(str(physician_id)))


def _request_weight(contract, weight):
    settings = contract.request_settings if isinstance(contract.request_settings, dict) else {}
    configured = _decimal_or_none(settings.get(f'weight_{weight.lower()}'))
    if configured is not None:
        return configured
    return Decimal('0')


def _requests_for_shift(requests_by_physician_date, physician_id, instance):
    """Return only requests that apply to this concrete dated shift instance.

    A Shift Off request may intentionally retain templates that do not run on
    its request date. Those templates are inert: only a template matching the
    current instance can influence construction, scoring, feasibility, or
    repair. Day-level requests continue to apply to every instance that day.
    """
    requests = requests_by_physician_date.get((physician_id, instance.date), [])
    matching = []
    for schedule_request in requests:
        if schedule_request.request_type in {
            ScheduleRequest.RequestType.DAY_OFF,
            ScheduleRequest.RequestType.DAY_ON,
        }:
            matching.append(schedule_request)
            continue
        template_ids = {
            template.id for template in schedule_request.shift_templates.all()
        }
        if instance.shift_template_id in template_ids:
            matching.append(schedule_request)
    return matching


def _request_score(schedule_requests, contract):
    """Return only outstanding penalty contributed by assigned shifts.

    Request-on fulfillment is intentionally zero here. Its preference during
    construction is handled separately, and an unmet request-on penalty is
    added once per request after all assignments have been inspected.
    """
    score = Decimal('0')
    request_violations = 0
    for schedule_request in schedule_requests:
        weight = _request_weight(contract, schedule_request.weight)
        if schedule_request.request_type in {
            ScheduleRequest.RequestType.DAY_OFF,
            ScheduleRequest.RequestType.SHIFT_OFF,
        }:
            score += weight
            request_violations += 1
    return score, request_violations, 0


def _request_candidate_rank(schedule_requests, contract):
    """Preserve request-on preference without subtracting from final score."""
    score, _violations, _rewards = _request_score(schedule_requests, contract)
    for schedule_request in schedule_requests:
        if (
            schedule_request.weight == ScheduleRequest.Weight.FIXED
            and schedule_request.request_type in {
                ScheduleRequest.RequestType.DAY_OFF,
                ScheduleRequest.RequestType.SHIFT_OFF,
            }
        ):
            # FIXED is an optimizer obligation even when the contract has no
            # configured numeric request penalty. Keep it out of the scalar
            # score, but always rank a non-conflicting candidate ahead of it.
            score += Decimal('1E+18')
        if schedule_request.request_type in {
            ScheduleRequest.RequestType.DAY_ON,
            ScheduleRequest.RequestType.SHIFT_ON,
        }:
            score -= _request_weight(contract, schedule_request.weight) / Decimal('2')
    return score


def _request_violation_row(schedule_request, physician, score, instance=None, violation_type=None):
    template_names = [
        template.generated_name()
        for template in schedule_request.shift_templates.all()
    ]
    row_type = violation_type
    if row_type is None:
        if schedule_request.request_type in {
            ScheduleRequest.RequestType.DAY_OFF,
            ScheduleRequest.RequestType.SHIFT_OFF,
        }:
            row_type = f'REQUEST_{schedule_request.request_type}_VIOLATION'
        else:
            row_type = f'REQUEST_{schedule_request.request_type}_REWARD'
    dates = [schedule_request.date.isoformat()]
    shift_instance_ids = [instance.id] if instance is not None else []
    return {
        'physician_id': schedule_request.physician_id,
        'physician': _physician_display_name(physician) if physician is not None else str(schedule_request.physician_id),
        'violation_type': row_type,
        'dates_involved': dates,
        'shift_instance_ids': shift_instance_ids,
        'facility': instance.facility.short_name or instance.facility.name if instance is not None else None,
        'shift_template': instance.shift_template.generated_name() if instance is not None else ', '.join(template_names),
        'request_id': schedule_request.id,
        'request_type': schedule_request.request_type,
        'request_scope': schedule_request.request_scope,
        'request_weight': schedule_request.weight,
        'configured_limit': schedule_request.request_type,
        'actual_value': 'assigned' if instance is not None else 'not assigned',
        'penalty_weight': float(abs(score)),
        'penalty': float(score),
        'explanation': (
            'Assigned shift conflicts with this request.'
            if score >= 0
            else 'Assigned shift satisfied this request and reduced request score.'
        ),
    }


def _request_scoring_rows(
    instances,
    physicians,
    state,
    contract_by_physician,
    requests_by_physician_date,
):
    instances_by_id = {instance.id: instance for instance in instances}
    physicians_by_id = {physician.id: physician for physician in physicians}
    rows = []
    for instance in instances:
        for physician_id in state[instance.id]:
            contract = contract_by_physician.get(physician_id)
            if contract is None:
                continue
            physician = physicians_by_id.get(physician_id)
            for schedule_request in _requests_for_shift(
                requests_by_physician_date,
                physician_id,
                instance,
            ):
                weight = _request_weight(contract, schedule_request.weight)
                if schedule_request.request_type in {
                    ScheduleRequest.RequestType.DAY_OFF,
                    ScheduleRequest.RequestType.SHIFT_OFF,
                }:
                    rows.append(
                        _request_violation_row(
                            schedule_request,
                            physician,
                            weight,
                            instance=instance,
                        )
                    )

    for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
        physician_instance_ids = [
            instance_id
            for instance_id, physician_ids in state.items()
            if physician_id in physician_ids
        ]
        physician_instances = [
            instances_by_id[instance_id]
            for instance_id in physician_instance_ids
            if instance_id in instances_by_id
        ]
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        physician = physicians_by_id.get(physician_id)
        for schedule_request in schedule_requests:
            if schedule_request.request_type == ScheduleRequest.RequestType.DAY_ON:
                if not any(instance.date == request_date for instance in physician_instances):
                    rows.append(
                        _request_violation_row(
                            schedule_request,
                            physician,
                            _request_weight(contract, schedule_request.weight),
                            violation_type='REQUEST_DAY_ON_UNMET',
                        )
                    )
            elif schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON:
                template_ids = {template.id for template in schedule_request.shift_templates.all()}
                if not any(
                    instance.date == request_date
                    and instance.shift_template_id in template_ids
                    for instance in physician_instances
                ):
                    rows.append(
                        _request_violation_row(
                            schedule_request,
                            physician,
                            _request_weight(contract, schedule_request.weight),
                            violation_type='REQUEST_SHIFT_ON_UNMET',
                        )
                    )
    return rows


def _workload_candidate_score(target, next_hours, next_shifts):
    target_value = target['target'] or Decimal('0')
    if target['units'] == 'SHIFTS':
        workload_value = Decimal(next_shifts)
        projected_total_score = Decimal(next_shifts) * Decimal('50')
    else:
        workload_value = next_hours
        projected_total_score = next_hours * Decimal('20')

    distance = workload_value - target_value
    over_target = max(distance, Decimal('0'))
    underutilized_bonus = (
        -ZERO_SHIFT_UNDERUTILIZATION_PENALTY
        if next_shifts == 1
        else Decimal('0')
    )
    return projected_total_score + (distance * distance * Decimal('5')) + (
        over_target * over_target * Decimal('25')
    ) + underutilized_bonus


def _underutilization_score(physicians, physician_shifts, eligible_facilities_by_physician):
    score = Decimal('0')
    for physician in physicians:
        if physician_shifts[physician.id] == 0 and eligible_facilities_by_physician.get(physician.id):
            score += ZERO_SHIFT_UNDERUTILIZATION_PENALTY
    return score


def _workload_value(target, physician_hours, physician_shifts, physician_id):
    return (
        Decimal(physician_shifts[physician_id])
        if target['units'] == 'SHIFTS'
        else physician_hours[physician_id]
    )


def _workload_schedule_score(target, assigned_hours, assigned_shifts):
    score = Decimal('0')
    for rule in target.get('rules') or []:
        workload_value = (
            Decimal(assigned_shifts)
            if rule['units'] == 'SHIFTS'
            else assigned_hours
        )
        if rule['min_value'] is not None and workload_value < rule['min_value']:
            score += (rule['min_value'] - workload_value) * rule['min_penalty_weight']
        if rule['max_value'] is not None and workload_value > rule['max_value']:
            score += (workload_value - rule['max_value']) * rule['max_penalty_weight']
    return score


def _workload_score_for_instances(all_instances, assigned_instances, target):
    score = Decimal('0')
    for rule in target.get('rules') or []:
        for window_start, window_end in _period_windows(all_instances, rule['period_type']):
            effective_rule = _effective_workload_rule(rule, window_start, window_end)
            window_instances = [
                instance
                for instance in assigned_instances
                if window_start <= instance.date <= window_end
            ]
            assigned_value = (
                Decimal(len(window_instances))
                if effective_rule['units'] == 'SHIFTS'
                else sum((_shift_hours(instance) for instance in window_instances), Decimal('0'))
            )
            if effective_rule['min_value'] is not None and assigned_value < effective_rule['min_value']:
                score += (
                    effective_rule['min_value'] - assigned_value
                ) * effective_rule['min_penalty_weight']
            if effective_rule['max_value'] is not None and assigned_value > effective_rule['max_value']:
                score += (
                    assigned_value - effective_rule['max_value']
                ) * effective_rule['max_penalty_weight']
    return score


def _workload_rule_delta_for_candidate(instances, state, instances_by_id, physician_id, instance, target):
    if not target.get('rules'):
        return Decimal('0')
    assigned_instances = [
        instances_by_id[assigned_instance_id]
        for assigned_instance_id, physician_ids in state.items()
        if physician_id in physician_ids
        and assigned_instance_id in instances_by_id
    ]
    before = _workload_score_for_instances(instances, assigned_instances, target)
    after = _workload_score_for_instances(instances, [*assigned_instances, instance], target)
    return after - before


def _workload_rule_delta_from_totals(range_rows, totals, instance_date, shift_hours):
    """Calculate an initial-fill workload delta from maintained window totals."""
    delta = Decimal('0')
    for row in range_rows:
        if not (row['window_start'] <= instance_date <= row['window_end']):
            continue
        key = (row['window_start'], row['window_end'], row['units'])
        before = totals.get(key, Decimal('0'))
        increment = Decimal('1') if row['units'] == 'SHIFTS' else shift_hours
        after = before + increment

        def penalty(value):
            score = Decimal('0')
            if row['min_value'] is not None and value < row['min_value']:
                score += (row['min_value'] - value) * row['min_penalty_weight']
            if row['max_value'] is not None and value > row['max_value']:
                score += (value - row['max_value']) * row['max_penalty_weight']
            return score

        delta += penalty(after) - penalty(before)
    return delta


def _night_volume_delta_from_totals(range_rows, totals, instance_date):
    """Calculate a night-volume delta from maintained period counts."""
    delta = Decimal('0')
    for row in range_rows:
        if not (row['window_start'] <= instance_date <= row['window_end']):
            continue
        key = (row['window_start'], row['window_end'])
        before = totals.get(key, Decimal('0'))
        after = before + Decimal('1')

        def penalty(value):
            score = Decimal('0')
            if row['min_shifts'] is not None and value < row['min_shifts']:
                score += (row['min_shifts'] - value) * row['min_penalty_weight']
            if row['max_shifts'] is not None and value > row['max_shifts']:
                score += (value - row['max_shifts']) * row['max_penalty_weight']
            return score

        delta += penalty(after) - penalty(before)
    return delta


def _night_volume_pressure_from_totals(range_rows, totals, instance_date):
    """Rank night candidates by proportional use of their configured capacity."""
    applicable = [
        row for row in range_rows
        if row['window_start'] <= instance_date <= row['window_end']
    ]
    if not applicable:
        return Decimal('0')
    pressures = []
    for row in applicable:
        key = (row['window_start'], row['window_end'])
        count = totals.get(key, Decimal('0'))
        if row['max_shifts'] is not None and row['max_shifts'] > 0:
            pressures.append(count / row['max_shifts'])
        else:
            pressures.append(count)
    return max(pressures, default=Decimal('0'))


def _initial_fill_workload_guard(range_rows, totals, shift_hours):
    """Return an ordinal rank and debug details for one initial-fill candidate."""
    applicable = [
        row for row in range_rows
        if row['window_start'] <= totals['date'] <= row['window_end']
    ]
    if not applicable:
        return 1, None

    evaluations = []
    for row in applicable:
        key = (row['window_start'], row['window_end'], row['units'])
        before = totals['values'].get(key, Decimal('0'))
        after = before + (Decimal('1') if row['units'] == 'SHIFTS' else shift_hours)
        evaluations.append((row, before, after))

    above = [item for item in evaluations if item[0]['max_value'] is not None and item[2] > item[0]['max_value']]
    below = [item for item in evaluations if item[0]['min_value'] is not None and item[1] < item[0]['min_value']]
    selected = (above or below or evaluations)[0]
    row, before, after = selected
    penalty = 2 if above else (0 if below else 1)
    return penalty, {
        'before': float(before),
        'after': float(after),
        'effective_range': {
            'period_type': row['period_type'],
            'period_start': row['window_start'].isoformat(),
            'period_end': row['window_end'].isoformat(),
            'units': row['units'],
            'min_value': float(row['min_value']) if row['min_value'] is not None else None,
            'max_value': float(row['max_value']) if row['max_value'] is not None else None,
        },
        'ranking_penalty': penalty,
    }


def _initial_fill_workload_scarcity(range_rows, totals, remaining_opportunities, instance_date):
    """Rank unmet period minima by the fraction of eligible capacity still needed.

    Opportunities are an optimistic upper bound: they respect the configured
    period and facility eligibility, but not future rest conflicts.  The value
    is bounded so an impossible minimum cannot monopolize every open shift.
    """
    urgency = Decimal('0')
    for index, row in enumerate(range_rows):
        minimum = row['min_value']
        if minimum is None or not (row['window_start'] <= instance_date <= row['window_end']):
            continue
        key = (row['window_start'], row['window_end'], row['units'])
        deficit = max(minimum - totals.get(key, Decimal('0')), Decimal('0'))
        opportunity = remaining_opportunities.get(index, Decimal('0'))
        if deficit > 0 and opportunity > 0:
            urgency = max(urgency, min(deficit / opportunity, Decimal('1')))
    return urgency


def _workload_rule_delta_for_reassignment(instances, state, instances_by_id, from_physician_id, to_physician_id, instance_id, targets):
    trial_state = _copy_state(state)
    _replace_in_state(trial_state, instance_id, from_physician_id, to_physician_id)
    score_before = Decimal('0')
    score_after = Decimal('0')
    for physician_id in {from_physician_id, to_physician_id}:
        target = targets.get(physician_id, {})
        if not target.get('rules'):
            continue
        before_instances = [
            instances_by_id[assigned_instance_id]
            for assigned_instance_id, physician_ids in state.items()
            if physician_id in physician_ids
            and assigned_instance_id in instances_by_id
        ]
        after_instances = [
            instances_by_id[assigned_instance_id]
            for assigned_instance_id, physician_ids in trial_state.items()
            if physician_id in physician_ids
            and assigned_instance_id in instances_by_id
        ]
        score_before += _workload_score_for_instances(instances, before_instances, target)
        score_after += _workload_score_for_instances(instances, after_instances, target)
    return score_after - score_before


def _workload_score_rows(physicians, instances, state, physician_hours, physician_shifts, physician_night_shifts, targets):
    rows = []
    assigned_by_physician = defaultdict(list)
    for instance in instances:
        for physician_id in state[instance.id]:
            assigned_by_physician[physician_id].append(instance)

    for physician in physicians:
        target = targets.get(physician.id)
        target_units = target['units'] if target else 'HOURS'
        target_value = target['target'] if target else Decimal('0')
        assigned_hours = physician_hours[physician.id]
        assigned_shifts = physician_shifts[physician.id]
        rule_rows = []
        score = Decimal('0')
        rules = (target or {}).get('rules') or []
        for rule in rules:
            for window_start, window_end in _period_windows(instances, rule['period_type']):
                effective_rule = _effective_workload_rule(rule, window_start, window_end)
                window_instances = [
                    instance
                    for instance in assigned_by_physician[physician.id]
                    if window_start <= instance.date <= window_end
                ]
                assigned_value = (
                    Decimal(len(window_instances))
                    if effective_rule['units'] == 'SHIFTS'
                    else sum((_shift_hours(instance) for instance in window_instances), Decimal('0'))
                )
                deviation = Decimal('0')
                penalty_weight = Decimal('0')
                direction = 'inside_range'
                contribution = Decimal('0')
                if effective_rule['min_value'] is not None and assigned_value < effective_rule['min_value']:
                    deviation = effective_rule['min_value'] - assigned_value
                    penalty_weight = effective_rule['min_penalty_weight']
                    contribution = deviation * penalty_weight
                    direction = 'below_minimum'
                elif effective_rule['max_value'] is not None and assigned_value > effective_rule['max_value']:
                    deviation = assigned_value - effective_rule['max_value']
                    penalty_weight = effective_rule['max_penalty_weight']
                    contribution = deviation * penalty_weight
                    direction = 'above_maximum'
                score += contribution
                rule_rows.append(
                    {
                        'period_type': rule['period_type'],
                        'period_start': window_start.isoformat(),
                        'period_end': window_end.isoformat(),
                        'units': effective_rule['units'],
                        'assigned_value': float(assigned_value),
                        'raw_min_value': float(effective_rule['raw_min_value']) if effective_rule['raw_min_value'] is not None else None,
                        'raw_max_value': float(effective_rule['raw_max_value']) if effective_rule['raw_max_value'] is not None else None,
                        'min_value': float(effective_rule['min_value']) if effective_rule['min_value'] is not None else None,
                        'max_value': float(effective_rule['max_value']) if effective_rule['max_value'] is not None else None,
                        'effective_min_value': float(effective_rule['min_value']) if effective_rule['min_value'] is not None else None,
                        'effective_max_value': float(effective_rule['max_value']) if effective_rule['max_value'] is not None else None,
                        'proration': effective_rule['proration'],
                        'debug_warning': effective_rule['debug_warning'],
                        'deviation': float(deviation),
                        'deviation_direction': direction,
                        'penalty_weight': float(penalty_weight),
                        'score_contribution': float(contribution),
                        'score_contribution_exact': str(contribution),
                    }
                )
        primary_rule = rule_rows[0] if rule_rows else None
        rows.append(
            {
                'physician_id': physician.id,
                'physician': _physician_display_name(physician),
                'contract_id': target.get('contract_id') if target else None,
                'contract_name': target.get('contract_name') if target else None,
                'assigned_shifts': assigned_shifts,
                'assigned_hours': float(assigned_hours),
                'night_shifts': physician_night_shifts[physician.id],
                'target_units': target_units,
                'target_shifts': None if rules else (float(target_value) if target_units == 'SHIFTS' else None),
                'target_hours': None if rules else (float(target_value) if target_units == 'HOURS' else None),
                'expected_target': float(target_value) if target_value is not None else None,
                'period_type': primary_rule['period_type'] if primary_rule else None,
                'raw_allowed_min': primary_rule['raw_min_value'] if primary_rule else None,
                'raw_allowed_max': primary_rule['raw_max_value'] if primary_rule else None,
                'allowed_min': primary_rule['min_value'] if primary_rule else None,
                'allowed_max': primary_rule['max_value'] if primary_rule else None,
                'allowed_units': primary_rule['units'] if primary_rule else target_units,
                'deviation': float(sum((Decimal(str(row['deviation'])) for row in rule_rows), Decimal('0'))),
                'deviation_direction': (
                    next((row['deviation_direction'] for row in rule_rows if row['deviation_direction'] != 'inside_range'), 'inside_range')
                ),
                'penalty_weight': (
                    next((row['penalty_weight'] for row in rule_rows if row['deviation_direction'] != 'inside_range'), 0)
                ),
                'shift_deviation': None if rules else (
                    float(Decimal(assigned_shifts) - target_value)
                    if target_units == 'SHIFTS'
                    else None
                ),
                'hour_deviation': None if rules else (
                    float(assigned_hours - target_value)
                    if target_units == 'HOURS'
                    else None
                ),
                'score_contribution': float(score),
                'score_contribution_exact': str(score),
                'rule_rows': rule_rows,
            }
        )
    return rows


def _workload_range_from_rule(rule):
    if not rule:
        return None
    return {
        'period_type': rule['period_type'], 'period_start': rule['period_start'],
        'period_end': rule['period_end'], 'units': rule['units'],
        'raw_min_value': rule['raw_min_value'], 'raw_max_value': rule['raw_max_value'],
        'min_value': rule['effective_min_value'], 'max_value': rule['effective_max_value'],
        'proration': rule['proration'], 'debug_warning': rule['debug_warning'],
    }


def _build_workload_summary(physicians, physician_hours, physician_shifts, physician_night_shifts, targets, workload_score_rows=None):
    summary = []
    workload_rows_by_physician = {
        row['physician_id']: row
        for row in (workload_score_rows or [])
    }
    for physician in physicians:
        target = targets.get(physician.id)
        target_value = target['target'] if target else None
        workload_row = workload_rows_by_physician.get(physician.id)
        primary_rule = (
            workload_row.get('rule_rows', [None])[0]
            if workload_row and workload_row.get('rule_rows')
            else None
        )
        summary.append(
            {
                'physician_id': physician.id,
                'physician_name': _physician_display_name(physician),
                'contract_id': target.get('contract_id') if target else None,
                'contract_name': target.get('contract_name') if target else None,
                'assigned_hours': float(physician_hours[physician.id]),
                'assigned_shifts': physician_shifts[physician.id],
                'night_shifts': physician_night_shifts[physician.id],
                'target_units': target['units'] if target else None,
                'target': float(target_value) if target_value is not None else None,
                'raw_workload_rule': primary_rule,
                'effective_workload_range': _workload_range_from_rule(primary_rule),
                'deviation': workload_row.get('deviation') if workload_row else 0.0,
                'deviation_direction': workload_row.get('deviation_direction') if workload_row else 'inside_range',
                'score_contribution': (
                    workload_row['score_contribution']
                    if workload_row is not None
                    else 0.0
                ),
            }
        )
    return summary


def _version_shift_instances_queryset(version):
    return ScheduleShiftInstance.objects.filter(
        schedule_version=version,
        date__gte=version.schedule_block.start_date,
        date__lte=version.schedule_block.end_date,
    )


def _unfilled_slot_count(instances, assigned_instance_physicians):
    return sum(
        max(instance.required_staffing - len(assigned_instance_physicians[instance.id]), 0)
        for instance in instances
    )


def _state_from_assignments(assignments):
    state = defaultdict(list)
    manual_pairs = set()
    for assignment in assignments:
        if assignment.physician_id not in state[assignment.shift_instance_id]:
            state[assignment.shift_instance_id].append(assignment.physician_id)
        if (
            assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
        ):
            manual_pairs.add((assignment.shift_instance_id, assignment.physician_id))
    return state, manual_pairs


def canonical_assignment_snapshot(
    assignments, instances, selected_run=None, preserve_physician_ids=None,
):
    """Deduplicate and cap an assignment snapshot without mutating source rows."""
    preserve_physician_ids = set(preserve_physician_ids or ())
    required_by_instance = {
        instance.id: instance.required_staffing for instance in instances
    }
    selected_run_id = getattr(selected_run, 'id', None)

    def priority(assignment):
        locked_manual = (
            assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
        )
        return (
            0 if locked_manual else 1,
            0 if assignment.optimizer_run_id == selected_run_id else 1,
            0 if assignment.optimizer_run_id is not None else 1,
            assignment.id or 0,
        )

    kept = []
    seen_pairs = set()
    counts_by_instance = defaultdict(int)
    duplicate_rows = []
    excess_rows = []
    for assignment in sorted(assignments, key=priority):
        pair = (assignment.shift_instance_id, assignment.physician_id)
        if pair in seen_pairs:
            duplicate_rows.append(assignment)
            continue
        required = required_by_instance.get(assignment.shift_instance_id)
        if (
            required is None
            or (
                counts_by_instance[assignment.shift_instance_id] >= required
                and assignment.physician_id not in preserve_physician_ids
            )
        ):
            excess_rows.append(assignment)
            continue
        seen_pairs.add(pair)
        counts_by_instance[assignment.shift_instance_id] += 1
        kept.append(assignment)
    return kept, {
        'duplicate_rows_discarded': len(duplicate_rows),
        'excess_rows_discarded': len(excess_rows),
        'discarded_assignment_ids': [
            assignment.id for assignment in [*duplicate_rows, *excess_rows]
            if assignment.id is not None
        ],
    }


def _invalid_state_assignment_capacity(instances, state, allowed_overstaff_physician_ids=None):
    allowed_overstaff_physician_ids = set(allowed_overstaff_physician_ids or ())
    issues = []
    for instance in instances:
        physician_ids = state[instance.id]
        duplicate_count = len(physician_ids) - len(set(physician_ids))
        excess_count = max(len(physician_ids) - instance.required_staffing, 0)
        disallowed_excess_count = max(
            len([
                physician_id for physician_id in physician_ids
                if physician_id not in allowed_overstaff_physician_ids
            ]) - instance.required_staffing,
            0,
        )
        if duplicate_count or disallowed_excess_count:
            issues.append({
                'shift_instance_id': instance.id,
                'duplicate_count': duplicate_count,
                'excess_count': excess_count,
                'disallowed_excess_count': disallowed_excess_count,
                'required_staffing': instance.required_staffing,
            })
    return issues


def _intervals_for_physician(state, instances_by_id, physician_id, exclude_instance_id=None):
    intervals = []
    for instance_id, physician_ids in state.items():
        if instance_id == exclude_instance_id:
            continue
        if physician_id not in physician_ids:
            continue
        instance = instances_by_id.get(instance_id)
        if instance is None:
            continue
        intervals.append((instance.start_datetime, instance.end_datetime))
    sample_instance = next(iter(instances_by_id.values()), None)
    if sample_instance is not None:
        intervals.extend(
            (prior.start_datetime, prior.end_datetime)
            for prior in getattr(
                sample_instance, '_published_boundary_context', {}
            ).get(physician_id, ())
        )
    return intervals


def _can_assign_in_state(
    state,
    instances_by_id,
    instance,
    physician_id,
    eligible_facilities_by_physician,
    minimum_rest_by_physician,
    exclude_instance_id=None,
    assigned_intervals=None,
):
    if physician_id in state[instance.id] and exclude_instance_id != instance.id:
        return False
    if instance.facility_id not in eligible_facilities_by_physician.get(physician_id, set()):
        return False

    intervals = assigned_intervals
    if isinstance(intervals, _AssignmentIntervalIndex):
        return not intervals.conflicts(instance, minimum_rest_by_physician[physician_id])
    if intervals is None:
        intervals = _intervals_for_physician(
            state,
            instances_by_id,
            physician_id,
            exclude_instance_id=exclude_instance_id,
        )
    if _overlaps(instance, intervals):
        return False
    return not _rest_violation(
        instance,
        intervals,
        minimum_rest_by_physician[physician_id],
    )


def _add_to_state(state, instance_id, physician_id):
    state[instance_id].append(physician_id)


def _replace_in_state(state, instance_id, old_physician_id, new_physician_id):
    state[instance_id] = [
        new_physician_id if physician_id == old_physician_id else physician_id
        for physician_id in state[instance_id]
    ]


def _current_violation_assignment_pairs(violation, state, manual_pairs):
    """Return movable assignments, ignoring immutable boundary-context shifts."""
    physician_id = violation['physician_id']
    return [
        (instance_id, physician_id)
        for instance_id in violation.get('shift_instance_ids') or []
        if physician_id in state.get(instance_id, ())
        and (instance_id, physician_id) not in manual_pairs
    ]


def _validate_schedule(
    instances,
    physicians,
    state,
    eligible_facilities_by_physician,
    minimum_rest_by_physician,
    manual_assignment_only_physician_ids=None,
):
    manual_assignment_only_physician_ids = set(manual_assignment_only_physician_ids or ())
    active_physician_ids = {physician.id for physician in physicians if physician.active}
    instances_by_id = {instance.id: instance for instance in instances}
    intervals_by_physician = defaultdict(list)
    duplicate_violations = 0
    overstaffed_violations = 0
    inactive_physician_violations = 0
    facility_ineligible_violations = 0

    for instance in instances:
        physician_ids = list(state[instance.id])
        duplicate_violations += len(physician_ids) - len(set(physician_ids))
        overstaffed_violations += max(len(physician_ids) - instance.required_staffing, 0)
        for physician_id in physician_ids:
            if physician_id not in active_physician_ids:
                inactive_physician_violations += 1
            if (
                physician_id not in manual_assignment_only_physician_ids
                and instance.facility_id not in eligible_facilities_by_physician.get(physician_id, set())
            ):
                facility_ineligible_violations += 1
            intervals_by_physician[physician_id].append(instance.id)

    overlap_violations = 0
    rest_violations = 0
    boundary_context = _published_boundary_context(instances)
    for physician_id, instance_ids in intervals_by_physician.items():
        if physician_id in manual_assignment_only_physician_ids:
            continue
        physician_instances = sorted(
            (
                instances_by_id[instance_id]
                for instance_id in instance_ids
                if instance_id in instances_by_id
            ),
            key=lambda instance: (instance.start_datetime, instance.end_datetime, instance.id),
        )
        minimum_rest = timedelta(
            hours=float(minimum_rest_by_physician.get(physician_id, DEFAULT_MIN_REST_HOURS))
        )
        for index, left in enumerate(physician_instances):
            for right in physician_instances[index + 1:]:
                if right.start_datetime < left.end_datetime and right.end_datetime > left.start_datetime:
                    overlap_violations += 1
                    continue
                if left.end_datetime <= right.start_datetime:
                    rest_gap = right.start_datetime - left.end_datetime
                else:
                    rest_gap = left.start_datetime - right.end_datetime
                if rest_gap < minimum_rest:
                    rest_violations += 1

        # The prior schedule is immutable context.  Only conflicts involving
        # a current-block assignment are charged to this schedule.
        for current in physician_instances:
            for prior in boundary_context.get(physician_id, ()):
                if (
                    current.start_datetime < prior.end_datetime
                    and current.end_datetime > prior.start_datetime
                ):
                    overlap_violations += 1
                    continue
                if prior.end_datetime <= current.start_datetime:
                    rest_gap = current.start_datetime - prior.end_datetime
                else:
                    rest_gap = prior.start_datetime - current.end_datetime
                if rest_gap < minimum_rest:
                    rest_violations += 1

    return {
        'final_overlap_violations': overlap_violations,
        'final_rest_violations': rest_violations,
        'final_duplicate_violations': duplicate_violations,
        'final_overstaffed_violations': overstaffed_violations,
        'final_inactive_physician_violations': inactive_physician_violations,
        'final_facility_ineligible_violations': facility_ineligible_violations,
    }


def _streak_excess_score(sorted_dates, max_streak, penalty):
    if not sorted_dates:
        return Decimal('0')
    score = Decimal('0')
    streak = 1
    previous_date = sorted_dates[0]
    for current_date in sorted_dates[1:]:
        if current_date == previous_date:
            continue
        if current_date == previous_date + timedelta(days=1):
            streak += 1
        else:
            streak = 1
        if streak > max_streak:
            score += penalty
        previous_date = current_date
    return score


def _workload_rule_limit(contract, key, default):
    settings = contract.workload_settings if isinstance(contract.workload_settings, dict) else {}
    return _positive_int_or_default(settings.get(key), default)


def _workload_rule_penalty(contract, key, default):
    settings = contract.workload_settings if isinstance(contract.workload_settings, dict) else {}
    return _positive_decimal_or_default(settings.get(key), default)


def _same_shift_rule(contract):
    settings = contract.workload_settings if isinstance(contract.workload_settings, dict) else {}
    limit = _decimal_or_none(settings.get('max_same_shifts_in_row'))
    penalty = _decimal_or_none(settings.get('max_same_shifts_in_row_penalty_weight'))
    if limit is None or limit <= 0 or penalty is None or penalty <= 0:
        return None
    return max(int(limit), 1), penalty


def _night_rule_limit(contract, key, default):
    settings = contract.night_settings if isinstance(contract.night_settings, dict) else {}
    return _positive_int_or_default(settings.get(key), default)


def _night_rule_penalty(contract, key, default):
    settings = contract.night_settings if isinstance(contract.night_settings, dict) else {}
    return _positive_decimal_or_default(settings.get(key), default)


def _night_settings(contract):
    return contract.night_settings if isinstance(contract.night_settings, dict) else {}


def _contract_rule_identity(contract):
    if contract is None:
        return {
            'contract_id': None,
            'contract_name': None,
        }
    return {
        'contract_id': contract.id,
        'contract_name': contract.name,
    }


def _night_rules_debug_payload(contract):
    settings = _night_settings(contract)
    return {
        'contract_id': contract.id,
        'contract_name': contract.name,
        'period_rules': _unique_night_period_rules(settings),
        'min_consecutive_night_shifts': _configured_positive_int(
            settings,
            'min_consecutive_night_shifts',
        ),
        'min_consecutive_night_shifts_penalty_weight': float(
            _configured_positive_penalty(
                settings,
                'min_consecutive_night_shifts_penalty_weight',
                DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
            )
        ),
        'max_consecutive_night_shifts': _configured_positive_int(
            settings,
            'max_consecutive_night_shifts',
        ),
        'max_consecutive_night_shifts_penalty_weight': float(
            _configured_positive_penalty(
                settings,
                'max_consecutive_night_shifts_penalty_weight',
                DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
            )
        ),
        'days_off_after_night_block': _configured_positive_int(
            settings,
            'days_off_after_night_block',
        ),
        'days_off_after_night_block_penalty_weight': float(
            _configured_positive_penalty(
                settings,
                'days_off_after_night_block_penalty_weight',
                DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
            )
        ),
        'days_off_before_next_night_shift': _configured_positive_int(
            settings,
            'days_off_before_next_night_shift',
        ),
        'days_off_before_next_night_shift_penalty_weight': float(
            _configured_positive_penalty(
                settings,
                'days_off_before_next_night_shift_penalty_weight',
                DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
            )
        ),
    }


def _unique_night_period_rules(settings):
    rules = []
    seen = set()
    for rule in settings.get('period_rules') or []:
        if not isinstance(rule, dict):
            continue
        min_shifts = _decimal_or_none(rule.get('min_shifts'))
        max_shifts = _decimal_or_none(rule.get('max_shifts'))
        if min_shifts is None and max_shifts is None:
            continue
        period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
        min_penalty = _decimal_or_none(rule.get('min_penalty_weight'))
        max_penalty = _decimal_or_none(rule.get('max_penalty_weight'))
        key = (
            period_type,
            str(min_shifts) if min_shifts is not None else '',
            str(max_shifts) if max_shifts is not None else '',
            str(min_penalty) if min_penalty is not None else '',
            str(max_penalty) if max_penalty is not None else '',
        )
        if key in seen:
            continue
        seen.add(key)
        rules.append(rule)
    return rules


def _configured_positive_int(settings, key):
    value = _decimal_or_none(settings.get(key))
    if value is None or value <= 0:
        return None
    return int(value)


def _configured_positive_penalty(settings, key, default):
    value = _decimal_or_none(settings.get(key))
    if value is None or value < 0:
        return Decimal('0')
    return value


def _period_contains_date(period_type, period_start, period_end, current_date):
    if period_type == 'WEEK':
        week_start = current_date - timedelta(days=current_date.weekday())
        week_end = week_start + timedelta(days=6)
        return max(week_start, period_start), min(week_end, period_end)
    if period_type == 'MONTH':
        month_start = current_date.replace(day=1)
        next_month = (
            current_date.replace(year=current_date.year + 1, month=1, day=1)
            if current_date.month == 12
            else current_date.replace(month=current_date.month + 1, day=1)
        )
        month_end = next_month - timedelta(days=1)
        return max(month_start, period_start), min(month_end, period_end)
    return period_start, period_end


def _period_windows(instances, period_type):
    if not instances:
        return []
    schedule_block = getattr(instances[0], 'schedule_block', None)
    if schedule_block is not None:
        period_start = schedule_block.start_date
        period_end = schedule_block.end_date
    else:
        period_start = min(instance.date for instance in instances)
        period_end = max(instance.date for instance in instances)
    dates = tuple(sorted({instance.date for instance in instances}))
    # Cache values, never model objects or mutable schedule state. Changed
    # bounds/dates produce a new key; callers receive their own mutable list.
    return list(_period_windows_for_dates(period_start, period_end, dates, period_type))


@lru_cache(maxsize=128)
def _period_windows_for_dates(period_start, period_end, dates, period_type):
    return tuple(sorted({
        _period_contains_date(period_type, period_start, period_end, current_date)
        for current_date in dates
    }))


def _month_bounds(current_date):
    month_start = current_date.replace(day=1)
    next_month = (
        current_date.replace(year=current_date.year + 1, month=1, day=1)
        if current_date.month == 12
        else current_date.replace(month=current_date.month + 1, day=1)
    )
    return month_start, next_month - timedelta(days=1)


def _prorated_decimal(value, ratio, units, bound_type):
    if value is None:
        return None
    prorated = value * ratio
    if units == 'SHIFTS':
        rounding = ROUND_FLOOR if bound_type == 'min' else ROUND_CEILING
        return prorated.to_integral_value(rounding=rounding)
    return prorated.quantize(Decimal('0.01'))


def _effective_workload_rule(rule, window_start, window_end):
    effective_min = rule['min_value']
    effective_max = rule['max_value']
    proration = None
    debug_warning = None
    if rule['period_type'] == 'MONTH':
        month_start, month_end = _month_bounds(window_start)
        if window_start != month_start or window_end != month_end:
            days_in_window = Decimal((window_end - window_start).days + 1)
            days_in_month = Decimal((month_end - month_start).days + 1)
            ratio = days_in_window / days_in_month
            effective_min = _prorated_decimal(rule['min_value'], ratio, rule['units'], 'min')
            effective_max = _prorated_decimal(rule['max_value'], ratio, rule['units'], 'max')
            proration = {
                'period_days_in_schedule_block': int(days_in_window),
                'days_in_month': int(days_in_month),
                'ratio': float(ratio),
            }
            debug_warning = 'Month workload rule prorated for partial-month schedule block.'
    return {
        **rule,
        'raw_min_value': rule['min_value'],
        'raw_max_value': rule['max_value'],
        'min_value': effective_min,
        'max_value': effective_max,
        'proration': proration,
        'debug_warning': debug_warning,
    }


def _night_minimum_period_priority(period_type):
    priorities = {
        'SCHEDULE_BLOCK': 0,
        'MONTH': 1,
        'WEEK': 2,
    }
    return priorities.get(period_type or 'SCHEDULE_BLOCK', 99)


def _dedupe_night_minimum_rows(rows):
    kept_by_key = {}
    suppressed = []
    for row in sorted(
        rows,
        key=lambda item: (
            item['physician_id'],
            item['period_start'],
            item['period_end'],
            item['minimum'],
            _night_minimum_period_priority(item['period_type']),
        ),
    ):
        key = (
            row['physician_id'],
            row['period_start'],
            row['period_end'],
            row['minimum'],
        )
        kept = kept_by_key.get(key)
        if kept is None:
            kept_by_key[key] = row
            continue
        suppressed.append(
            {
                'physician_id': row['physician_id'],
                'physician': row['physician'],
                'contract_id': row.get('contract_id'),
                'contract_name': row.get('contract_name'),
                'suppressed_period_type': row['period_type'],
                'kept_period_type': kept['period_type'],
                'configured_minimum': row['minimum'],
                'period_start': row['period_start'],
                'period_end': row['period_end'],
                'reason': (
                    'Equivalent minimum-night rule for the same physician, effective date range, '
                    'and configured minimum was already applied.'
                ),
            }
        )
    return list(kept_by_key.values()), suppressed


class _NightWindowIndex:
    """Per-evaluation counts; never reuse across mutable candidate schedules."""

    def __init__(self, instances, state):
        instances_by_id = {instance.id: instance for instance in instances}
        self.dates = defaultdict(list)
        for instance_id, physician_ids in state.items():
            instance = instances_by_id.get(instance_id)
            if instance is None or not instance.shift_template.night_shift:
                continue
            # Match the reference counter: one count per shift/physician pair,
            # including distinct shifts on the same date, not duplicate IDs.
            for physician_id in set(physician_ids):
                self.dates[physician_id].append(instance.date)
        for dates in self.dates.values():
            dates.sort()

    def count(self, physician_id, start, end):
        dates = self.dates.get(physician_id, ())
        return max(0, bisect_right(dates, end) - bisect_left(dates, start))


def _night_minimum_rule_evaluation(instances, physicians, state, contract_by_physician, *, night_counts=None):
    if night_counts is None:
        night_counts = _NightWindowIndex(instances, state)
    candidate_rows = []
    required_values = []
    period_types = []
    for physician in physicians:
        contract = contract_by_physician.get(physician.id)
        if contract is None:
            continue
        for rule in _night_minimum_rules_for_contract(contract):
            required_values.append(rule['minimum'])
            period_types.append(rule['period_type'])
            for window_start, window_end in _period_windows(instances, rule['period_type']):
                count = night_counts.count(
                    physician.id,
                    window_start,
                    window_end,
                )
                candidate_rows.append(
                    {
                        'physician_id': physician.id,
                        'physician': _physician_display_name(physician),
                        **_contract_rule_identity(contract),
                        'period_type': rule['period_type'],
                        'period_start': window_start.isoformat(),
                        'period_end': window_end.isoformat(),
                        'minimum': rule['minimum'],
                        'actual': count,
                        'penalty_weight': rule['penalty_weight'],
                    }
                )

    applied_rows, suppressed_rows = _dedupe_night_minimum_rows(candidate_rows)
    under = [
        row
        for row in applied_rows
        if row['actual'] < row['minimum']
    ]
    over = [
        row
        for row in applied_rows
        if row['actual'] > row['minimum']
    ]
    return {
        'night_minimum_required': max(required_values) if required_values else 0,
        'night_minimum_period': ', '.join(sorted(set(period_types))) if period_types else None,
        'physicians_under_night_minimum': under,
        'physicians_over_night_minimum': over,
        'night_minimum_violations_count': len(under),
        'night_minimum_rules_applied': applied_rows,
        'night_minimum_rules_suppressed_as_duplicates': suppressed_rows,
    }


def _assignment_debug_payload(instance):
    return {
        'shift_instance_id': instance.id,
        'date': instance.date.isoformat(),
        'facility': instance.facility.short_name or instance.facility.name,
        'shift_template': instance.shift_template.name,
        'start_datetime': instance.start_datetime.isoformat(),
        'end_datetime': instance.end_datetime.isoformat(),
        'night_shift': bool(instance.shift_template.night_shift),
    }


def _night_blocks(night_instances):
    blocks = []
    current = []
    previous_date = None
    for instance in sorted(night_instances, key=lambda item: (item.date, item.start_datetime, item.id)):
        if previous_date is None or instance.date == previous_date + timedelta(days=1):
            current.append(instance)
        else:
            blocks.append(current)
            current = [instance]
        previous_date = instance.date
    if current:
        blocks.append(current)
    return blocks


def _block_dates(block):
    return [instance.date.isoformat() for instance in block]


def _full_calendar_days_between(left_date, right_date):
    return max((right_date - left_date).days - 1, 0)


def _night_block_extension_bonus(instances_by_id, state, contract_by_physician, physician_id, instance):
    if not instance.shift_template.night_shift:
        return Decimal('0')
    physician_nights = [
        instances_by_id[assigned_instance_id]
        for assigned_instance_id, physician_ids in state.items()
        if physician_id in physician_ids
        and assigned_instance_id in instances_by_id
        and instances_by_id[assigned_instance_id].shift_template.night_shift
    ]
    if not physician_nights:
        return Decimal('0')

    contract = contract_by_physician.get(physician_id)
    settings = _night_settings(contract) if contract is not None else {}
    max_consecutive = _configured_positive_int(
        settings,
        'max_consecutive_night_shifts',
    )
    min_consecutive = _configured_positive_int(
        settings,
        'min_consecutive_night_shifts',
    )
    projected_blocks = _night_blocks([*physician_nights, instance])
    for block in projected_blocks:
        dates = {item.date for item in block}
        if instance.date not in dates:
            continue
        if max_consecutive is not None and len(block) > max_consecutive:
            return Decimal('0')
        if (
            instance.date - timedelta(days=1) in dates
            or instance.date + timedelta(days=1) in dates
        ):
            days_after = _configured_positive_int(settings, 'days_off_after_night_block') or 0
            days_after_penalty = _configured_positive_penalty(
                settings,
                'days_off_after_night_block_penalty_weight',
                DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
            )
            min_consecutive_penalty = _configured_positive_penalty(
                settings,
                'min_consecutive_night_shifts_penalty_weight',
                DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
            )
            min_consecutive_bonus = (
                min_consecutive_penalty * Decimal(max(min_consecutive - len(block), 1))
                if min_consecutive is not None and len(block) <= min_consecutive
                else Decimal('0')
            )
            configured_guidance = max(
                days_after_penalty * Decimal(
                    min(days_after, max_consecutive or max(days_after, 1))
                ),
                min_consecutive_bonus,
            )
            return -configured_guidance
    return Decimal('0')


def _can_extend_night_block(instances_by_id, state, contract_by_physician, physician_id, instance):
    return _night_block_extension_bonus(
        instances_by_id,
        state,
        contract_by_physician,
        physician_id,
        instance,
    ) < 0


def _night_violation_report(
    instances,
    physicians,
    state,
    contract_by_physician,
    include_internal_heuristics=False,
    default_target_override=None,
):
    physicians_by_id = {physician.id: physician for physician in physicians}
    instances_by_id = {instance.id: instance for instance in instances}
    current_instance_ids = set(instances_by_id)
    boundary_context = _published_boundary_context(instances)
    assignments_by_physician = defaultdict(list)
    night_instances_by_physician = defaultdict(list)
    total_night_shifts = 0

    for instance_id, physician_ids in state.items():
        instance = instances_by_id.get(instance_id)
        if instance is None:
            continue
        for physician_id in physician_ids:
            assignments_by_physician[physician_id].append(instance)
            if instance.shift_template.night_shift:
                night_instances_by_physician[physician_id].append(instance)
                total_night_shifts += 1
    assigned_night_dates = {
        instance.date
        for night_instances in night_instances_by_physician.values()
        for instance in night_instances
    }

    score = Decimal('0')
    violations = []
    night_counts = {
        physician.id: len(night_instances_by_physician[physician.id])
        for physician in physicians
    }
    default_target = default_target_override if default_target_override is not None else (
        Decimal(total_night_shifts) / Decimal(len(physicians))
        if physicians
        else Decimal('0')
    )
    night_minimum_rules_applied = []
    night_minimum_rules_suppressed_as_duplicates = []
    night_counts_index = _NightWindowIndex(instances, state)

    for physician in physicians:
        physician_id = physician.id
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        settings = _night_settings(contract)
        night_instances = sorted(
            night_instances_by_physician[physician_id],
            key=lambda item: (item.date, item.start_datetime, item.id),
        )
        night_count = len(night_instances)
        prior_assignments = list(boundary_context.get(physician_id, ()))
        prior_night_instances = [
            instance for instance in prior_assignments
            if instance.shift_template.night_shift
        ]
        current_night_blocks = _night_blocks(night_instances)
        night_blocks = _night_blocks([*prior_night_instances, *night_instances])

        configured_volume_rule = False
        minimum_evaluation = _night_minimum_rule_evaluation(
            instances,
            [physician],
            state,
            contract_by_physician,
            night_counts=night_counts_index,
        )
        night_minimum_rules_applied.extend(minimum_evaluation['night_minimum_rules_applied'])
        night_minimum_rules_suppressed_as_duplicates.extend(
            minimum_evaluation['night_minimum_rules_suppressed_as_duplicates']
        )
        if minimum_evaluation['night_minimum_rules_applied']:
            configured_volume_rule = True
        for row in minimum_evaluation['physicians_under_night_minimum']:
            penalty_weight = Decimal(str(row['penalty_weight']))
            excess = row['minimum'] - row['actual']
            penalty = Decimal(excess) * penalty_weight
            score += penalty
            violations.append(
                {
                    'physician_id': physician_id,
                    'physician': _physician_display_name(physician),
                    **_contract_rule_identity(contract),
                    'violation_type': 'NIGHT_UNDER_MINIMUM',
                    'dates_involved': [],
                    'night_block_dates': [],
                    'period_type': row['period_type'],
                    'period_start': row['period_start'],
                    'period_end': row['period_end'],
                    'configured_limit': row['minimum'],
                    'actual_value': row['actual'],
                    'penalty_weight': float(penalty_weight),
                    'penalty': float(penalty),
                    'explanation': 'Configured minimum night-shift volume was not met for this period.',
                }
            )

        for rule in _unique_night_period_rules(settings):
            min_shifts = _decimal_or_none(rule.get('min_shifts'))
            max_shifts = _decimal_or_none(rule.get('max_shifts'))
            if min_shifts is None and max_shifts is None:
                continue
            configured_volume_rule = True
            period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
            for window_start, window_end in _period_windows(instances, period_type):
                count = sum(
                    1
                    for instance in night_instances
                    if window_start <= instance.date <= window_end
                )
                if max_shifts is not None and count > int(max_shifts):
                    penalty = _configured_positive_penalty(
                        rule,
                        'max_penalty_weight',
                        DEFAULT_NIGHT_BALANCE_PENALTY,
                    )
                    if penalty <= 0:
                        continue
                    excess = count - int(max_shifts)
                    score += Decimal(excess) * penalty
                    violations.append(
                        {
                            'physician_id': physician_id,
                            'physician': _physician_display_name(physician),
                            **_contract_rule_identity(contract),
                            'violation_type': 'NIGHT_OVER_MAXIMUM',
                            'dates_involved': [
                                instance.date.isoformat()
                                for instance in night_instances
                                if window_start <= instance.date <= window_end
                            ],
                            'shift_instance_ids': [
                                instance.id
                                for instance in night_instances
                                if window_start <= instance.date <= window_end
                            ],
                            'night_block_dates': [
                                _block_dates(block)
                                for block in current_night_blocks
                                if any(window_start <= instance.date <= window_end for instance in block)
                            ],
                            'period_type': period_type,
                            'period_start': window_start.isoformat(),
                            'period_end': window_end.isoformat(),
                            'configured_limit': int(max_shifts),
                            'actual_value': count,
                            'penalty_weight': float(penalty),
                            'penalty': float(Decimal(excess) * penalty),
                            'explanation': 'Configured maximum night-shift volume was exceeded for this period.',
                        }
                    )

        max_consecutive = _configured_positive_int(
            settings,
            'max_consecutive_night_shifts',
        )
        min_consecutive = _configured_positive_int(
            settings,
            'min_consecutive_night_shifts',
        )
        min_consecutive_penalty = _configured_positive_penalty(
            settings,
            'min_consecutive_night_shifts_penalty_weight',
            DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
        )
        consecutive_penalty = _configured_positive_penalty(
            settings,
            'max_consecutive_night_shifts_penalty_weight',
            DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
        )
        for block in night_blocks:
            current_block_instances = [
                instance for instance in block
                if instance.id in current_instance_ids
            ]
            if not current_block_instances:
                continue
            if (
                min_consecutive is not None
                and min_consecutive_penalty > 0
                and len(block) < min_consecutive
            ):
                shortfall = min_consecutive - len(block)
                penalty = Decimal(shortfall) * min_consecutive_penalty
                score += penalty
                violations.append(
                    {
                        'physician_id': physician_id,
                        'physician': _physician_display_name(physician),
                        **_contract_rule_identity(contract),
                        'violation_type': 'MIN_CONSECUTIVE_NIGHTS',
                        'dates_involved': _block_dates(block),
                        'night_block_dates': _block_dates(block),
                        'night_block_assignments': [
                            _assignment_debug_payload(instance)
                            for instance in block
                        ],
                        'shift_instance_ids': [instance.id for instance in block],
                        'configured_limit': min_consecutive,
                        'actual_value': len(block),
                        'penalty_weight': float(min_consecutive_penalty),
                        'penalty': float(penalty),
                        'explanation': 'Night block is shorter than the configured minimum consecutive nights.',
                    }
                )
            if (
                max_consecutive is not None
                and consecutive_penalty > 0
                and len(block) > max_consecutive
            ):
                prior_count = len(block) - len(current_block_instances)
                prior_excess = max(prior_count - max_consecutive, 0)
                excess = max(len(block) - max_consecutive - prior_excess, 0)
                if not excess:
                    continue
                penalty = Decimal(excess) * consecutive_penalty
                score += penalty
                violations.append(
                    {
                        'physician_id': physician_id,
                        'physician': _physician_display_name(physician),
                        **_contract_rule_identity(contract),
                        'violation_type': 'MAX_CONSECUTIVE_NIGHTS',
                        'dates_involved': _block_dates(block),
                        'night_block_dates': _block_dates(block),
                        'night_block_assignments': [
                            _assignment_debug_payload(instance)
                            for instance in block
                        ],
                        'shift_instance_ids': [instance.id for instance in block],
                        'configured_limit': max_consecutive,
                        'actual_value': len(block),
                        'penalty_weight': float(consecutive_penalty),
                        'penalty': float(penalty),
                        'explanation': 'Night block is longer than the configured maximum consecutive nights.',
                    }
                )
            elif (
                include_internal_heuristics
                and
                len(block) == 1
                and (min_consecutive or max_consecutive or 0) > 1
                and max(min_consecutive_penalty, consecutive_penalty) > 0
                and (
                    block[0].date - timedelta(days=1) in assigned_night_dates
                    or block[0].date + timedelta(days=1) in assigned_night_dates
                )
            ):
                score += max(
                    min_consecutive_penalty, consecutive_penalty,
                ) * Decimal('2')

        assignments = sorted(
            [*prior_assignments, *assignments_by_physician[physician_id]],
            key=lambda item: (item.start_datetime, item.end_datetime, item.id),
        )
        days_after = _configured_positive_int(settings, 'days_off_after_night_block')
        days_after_penalty = _configured_positive_penalty(
            settings,
            'days_off_after_night_block_penalty_weight',
            DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
        )
        if days_after is not None and days_after_penalty > 0:
            for block in night_blocks:
                block_instance_ids = {instance.id for instance in block}
                block_end = block[-1]
                next_assignment = next(
                    (
                        assignment
                        for assignment in assignments
                        if assignment.id not in block_instance_ids
                        and not assignment.shift_template.night_shift
                        and assignment.start_datetime >= block_end.end_datetime
                    ),
                    None,
                )
                if next_assignment is None:
                    continue
                if next_assignment.id not in current_instance_ids:
                    continue
                actual_days_off = _full_calendar_days_between(block_end.date, next_assignment.date)
                if actual_days_off < days_after:
                    shortfall = days_after - actual_days_off
                    penalty = Decimal(shortfall) * days_after_penalty
                    score += penalty
                    violations.append(
                        {
                            'physician_id': physician_id,
                            'physician': _physician_display_name(physician),
                            **_contract_rule_identity(contract),
                            'violation_type': 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT',
                            'dates_involved': [
                                *_block_dates(block),
                                next_assignment.date.isoformat(),
                            ],
                            'night_block_dates': _block_dates(block),
                            'night_block_assignments': [
                                _assignment_debug_payload(instance)
                                for instance in block
                            ],
                            'shift_instance_ids': [
                                *[instance.id for instance in block],
                                next_assignment.id,
                            ],
                            'next_assignment': _assignment_debug_payload(next_assignment),
                            'assignment_details': [
                                *[
                                    _assignment_debug_payload(instance)
                                    for instance in block
                                ],
                                _assignment_debug_payload(next_assignment),
                            ],
                            'configured_limit': days_after,
                            'actual_value': actual_days_off,
                            'penalty_weight': float(days_after_penalty),
                            'penalty': float(penalty),
                            'explanation': 'Physician returned to a non-night shift too soon after a night block.',
                        }
                    )

        days_before_next_night_block = _configured_positive_int(settings, 'days_off_before_next_night_shift')
        days_before_next_night_block_penalty = _configured_positive_penalty(
            settings,
            'days_off_before_next_night_shift_penalty_weight',
            DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
        )
        if (
            days_before_next_night_block is not None
            and days_before_next_night_block_penalty > 0
        ):
            for prior_block, next_block in zip(night_blocks, night_blocks[1:]):
                if not any(
                    instance.id in current_instance_ids for instance in next_block
                ):
                    continue
                prior_block_end = prior_block[-1]
                next_block_start = next_block[0]
                actual_days_off = _full_calendar_days_between(prior_block_end.date, next_block_start.date)
                if actual_days_off < days_before_next_night_block:
                    shortfall = days_before_next_night_block - actual_days_off
                    penalty = Decimal(shortfall) * days_before_next_night_block_penalty
                    score += penalty
                    violations.append(
                        {
                            'physician_id': physician_id,
                            'physician': _physician_display_name(physician),
                            **_contract_rule_identity(contract),
                            'violation_type': 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK',
                            'dates_involved': [
                                *_block_dates(prior_block),
                                *_block_dates(next_block),
                            ],
                            'night_block_dates': [
                                _block_dates(prior_block),
                                _block_dates(next_block),
                            ],
                            'prior_night_block_dates': _block_dates(prior_block),
                            'next_night_block_dates': _block_dates(next_block),
                            'prior_night_block_assignments': [
                                _assignment_debug_payload(instance)
                                for instance in prior_block
                            ],
                            'next_night_block_assignments': [
                                _assignment_debug_payload(instance)
                                for instance in next_block
                            ],
                            'night_block_assignments': [
                                _assignment_debug_payload(instance)
                                for instance in [*prior_block, *next_block]
                            ],
                            'shift_instance_ids': [
                                *[instance.id for instance in prior_block],
                                *[instance.id for instance in next_block],
                            ],
                            'assignment_details': [
                                *[
                                    _assignment_debug_payload(instance)
                                    for instance in prior_block
                                ],
                                *[
                                    _assignment_debug_payload(instance)
                                    for instance in next_block
                                ],
                            ],
                            'configured_limit': days_before_next_night_block,
                            'actual_value': actual_days_off,
                            'penalty_weight': float(days_before_next_night_block_penalty),
                            'penalty': float(penalty),
                            'explanation': 'Physician started another night block too soon after the prior night block.',
                        }
                    )

    night_shifts_by_physician = [
        {
            'physician_id': physician.id,
            'physician': _physician_display_name(physician),
            'night_shifts': night_counts[physician.id],
        }
        for physician in physicians
    ]
    contracts_used_by_physician = [
        {
            'physician_id': physician.id,
            'physician': _physician_display_name(physician),
            **_contract_rule_identity(contract_by_physician.get(physician.id)),
        }
        for physician in physicians
    ]
    physicians_without_contract = [
        {
            'physician_id': physician.id,
            'physician': _physician_display_name(physician),
        }
        for physician in physicians
        if contract_by_physician.get(physician.id) is None
    ]
    contracts_by_id = {
        contract.id: contract
        for contract in contract_by_physician.values()
        if contract is not None
    }
    night_rules_by_contract = [
        _night_rules_debug_payload(contract)
        for contract in sorted(contracts_by_id.values(), key=lambda item: (item.name.lower(), item.id))
    ]
    night_block_candidates_by_physician = []
    for physician in physicians:
        contract = contract_by_physician.get(physician.id)
        settings = _night_settings(contract) if contract is not None else {}
        blocks = _night_blocks(night_instances_by_physician[physician.id])
        night_block_candidates_by_physician.append(
            {
                'physician_id': physician.id,
                'physician': _physician_display_name(physician),
                **_contract_rule_identity(contract),
                'min_consecutive_night_shifts': _configured_positive_int(
                    settings,
                    'min_consecutive_night_shifts',
                ),
                'max_consecutive_night_shifts': _configured_positive_int(
                    settings,
                    'max_consecutive_night_shifts',
                ),
                'assigned_blocks': [
                    {
                        'dates': _block_dates(block),
                        'length': len(block),
                        'shift_instance_ids': [instance.id for instance in block],
                    }
                    for block in blocks
                ],
            }
        )

    def violations_by_contract(violation_type):
        rows = defaultdict(lambda: {
            'contract_id': None,
            'contract_name': None,
            'violations': 0,
            'penalty': 0.0,
        })
        for violation in violations:
            if violation['violation_type'] != violation_type:
                continue
            key = violation.get('contract_id')
            row = rows[key]
            row['contract_id'] = violation.get('contract_id')
            row['contract_name'] = violation.get('contract_name')
            row['violations'] += 1
            row['penalty'] += float(violation.get('penalty', 0))
        return sorted(
            rows.values(),
            key=lambda item: ((item['contract_name'] or '').lower(), item['contract_id'] or 0),
        )

    violation_types = {violation['violation_type'] for violation in violations}
    unresolved_reasons = []
    if 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT' in violation_types:
        unresolved_reasons.append(
            'Configured days off after night blocks before non-night work are still violated by the final draft.'
        )
    if 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK' in violation_types:
        unresolved_reasons.append(
            'Configured days off after night blocks before another night block are still violated by the final draft.'
        )
    return {
        'score': score,
        'total_night_shifts': total_night_shifts,
        'night_shifts_by_physician': night_shifts_by_physician,
        'night_violations_count': len(violations),
        'night_violations': violations,
        'night_unresolved_reasons': unresolved_reasons,
        'max_nights_assigned_to_one_physician': max(night_counts.values()) if night_counts else 0,
        'night_minimum_rules_applied': night_minimum_rules_applied,
        'night_minimum_rules_suppressed_as_duplicates': night_minimum_rules_suppressed_as_duplicates,
        'contracts_used_by_physician': contracts_used_by_physician,
        'night_rules_by_contract': night_rules_by_contract,
        'physicians_without_contract': physicians_without_contract,
        'night_block_candidates_by_physician': night_block_candidates_by_physician,
        'night_minimum_violations_by_contract': violations_by_contract('NIGHT_UNDER_MINIMUM'),
        'night_maximum_violations_by_contract': violations_by_contract('NIGHT_OVER_MAXIMUM'),
        'min_consecutive_night_violations': [
            violation for violation in violations
            if violation['violation_type'] == 'MIN_CONSECUTIVE_NIGHTS'
        ],
        'max_consecutive_night_violations': [
            violation for violation in violations
            if violation['violation_type'] == 'MAX_CONSECUTIVE_NIGHTS'
        ],
        'post_night_to_non_night_recovery_violations': [
            violation for violation in violations
            if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT'
        ],
        'post_night_to_next_night_block_recovery_violations': [
            violation for violation in violations
            if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK'
        ],
    }


def _night_block_debug(instances, physicians, state, contract_by_physician):
    instances_by_id = {instance.id: instance for instance in instances}
    night_instances_by_physician = defaultdict(list)
    for instance_id, physician_ids in state.items():
        instance = instances_by_id.get(instance_id)
        if instance is None or not instance.shift_template.night_shift:
            continue
        for physician_id in physician_ids:
            night_instances_by_physician[physician_id].append(instance)

    physicians_by_id = {physician.id: physician for physician in physicians}
    blocks_by_physician = []
    block_lengths = []
    isolated_night_count = 0
    for physician in physicians:
        blocks = []
        for block in _night_blocks(night_instances_by_physician[physician.id]):
            block_lengths.append(len(block))
            if len(block) == 1:
                isolated_night_count += 1
            blocks.append(
                {
                    'dates': _block_dates(block),
                    'length': len(block),
                    'shift_instance_ids': [instance.id for instance in block],
                    'shift_template_ids': sorted({instance.shift_template_id for instance in block}),
                    'facilities': sorted(
                        {
                            instance.facility.short_name or instance.facility.name
                            for instance in block
                        }
                    ),
                }
            )
        if blocks:
            blocks_by_physician.append(
                {
                    'physician_id': physician.id,
                    'physician': _physician_display_name(physician),
                    **_contract_rule_identity(contract_by_physician.get(physician.id)),
                    'blocks': blocks,
                }
            )

    night_report = _night_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
    )
    post_recovery_count = sum(
        1
        for violation in night_report['night_violations']
        if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT'
    )
    next_night_block_recovery_count = sum(
        1
        for violation in night_report['night_violations']
        if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK'
    )
    return {
        'night_blocks_by_physician': blocks_by_physician,
        'night_blocks_assigned': blocks_by_physician,
        'isolated_night_count': isolated_night_count,
        'night_blocks_count': len(block_lengths),
        'average_night_block_length': (
            float(Decimal(sum(block_lengths)) / Decimal(len(block_lengths)))
            if block_lengths
            else 0.0
        ),
        'max_night_block_length': max(block_lengths) if block_lengths else 0,
        'post_night_recovery_violations_count': post_recovery_count,
        'next_night_block_recovery_violations_count': next_night_block_recovery_count,
    }


def _night_minimum_rules_for_contract(contract):
    settings = _night_settings(contract)
    rules = []
    for rule in _unique_night_period_rules(settings):
        min_shifts = _decimal_or_none(rule.get('min_shifts'))
        penalty_weight = _configured_positive_penalty(
            rule,
            'min_penalty_weight',
            DEFAULT_NIGHT_BALANCE_PENALTY,
        )
        if min_shifts is None or min_shifts <= 0 or penalty_weight <= 0:
            continue
        rules.append(
            {
                'period_type': rule.get('period_type') or 'SCHEDULE_BLOCK',
                'minimum': int(min_shifts),
                'penalty_weight': float(penalty_weight),
            }
        )
    return rules


def _night_count_for_window(instances_by_id, state, physician_id, window_start, window_end):
    return sum(
        1
        for instance_id, physician_ids in state.items()
        if physician_id in physician_ids
        and instance_id in instances_by_id
        and instances_by_id[instance_id].shift_template.night_shift
        and window_start <= instances_by_id[instance_id].date <= window_end
    )


def _night_minimum_status(instances, physicians, state, contract_by_physician):
    return _night_minimum_rule_evaluation(
        instances,
        physicians,
        state,
        contract_by_physician,
    )


def _night_minimum_candidate_bonus(instances, state, contract_by_physician, physician_id, instance):
    if not instance.shift_template.night_shift:
        return Decimal('0')
    contract = contract_by_physician.get(physician_id)
    if contract is None:
        return Decimal('0')
    instances_by_id = {candidate.id: candidate for candidate in instances}
    best_bonus = Decimal('0')
    for rule in _night_minimum_rules_for_contract(contract):
        for window_start, window_end in _period_windows(instances, rule['period_type']):
            if not (window_start <= instance.date <= window_end):
                continue
            count = _night_count_for_window(
                instances_by_id,
                state,
                physician_id,
                window_start,
                window_end,
            )
            if count < rule['minimum']:
                best_bonus = max(
                    best_bonus,
                    DEFAULT_NIGHT_MINIMUM_PRIORITY_BONUS * Decimal(rule['minimum'] - count),
                )
    return -best_bonus


def _night_recovery_conflict_pairs(instances, physicians, state, manual_pairs, contract_by_physician):
    report = _night_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
    )
    pairs = []
    for violation in report['night_violations']:
        if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT':
            next_assignment = violation.get('next_assignment') or {}
            instance_id = next_assignment.get('shift_instance_id')
        elif violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK':
            next_block = violation.get('next_night_block_assignments') or []
            instance_id = next_block[0].get('shift_instance_id') if next_block else None
        else:
            continue
        physician_id = violation['physician_id']
        if not instance_id or (instance_id, physician_id) in manual_pairs:
            continue
        pairs.append((physician_id, instance_id))
    return list(dict.fromkeys(pairs))


def _night_candidate_delta(instances, physicians, state, contract_by_physician, physician_id, instance):
    if not instance.shift_template.night_shift:
        return Decimal('0')
    current_report = _night_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
        include_internal_heuristics=True,
    )
    trial_state = _copy_state(state)
    _add_to_state(trial_state, instance.id, physician_id)
    projected_report = _night_violation_report(
        instances,
        physicians,
        trial_state,
        contract_by_physician,
        include_internal_heuristics=True,
    )
    return projected_report['score'] - current_report['score']


def _night_recovery_candidate_penalty(instances, physicians, state, contract_by_physician, physician_id, instance):
    if instance.shift_template.night_shift:
        return Decimal('0')
    contract = contract_by_physician.get(physician_id)
    if contract is None:
        return Decimal('0')
    settings = _night_settings(contract)
    days_after = _configured_positive_int(settings, 'days_off_after_night_block')
    days_after_penalty = _configured_positive_penalty(
        settings,
        'days_off_after_night_block_penalty_weight',
        DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
    )
    if days_after is None:
        return Decimal('0')

    instances_by_id = {candidate.id: candidate for candidate in instances}
    night_instances = [
        instances_by_id[assigned_instance_id]
        for assigned_instance_id, physician_ids in state.items()
        if physician_id in physician_ids
        and assigned_instance_id in instances_by_id
        and instances_by_id[assigned_instance_id].shift_template.night_shift
    ]
    penalty = Decimal('0')
    for block in _night_blocks(night_instances):
        if days_after is not None:
            block_end = block[-1]
            if instance.start_datetime >= block_end.end_datetime:
                actual_days_off = _full_calendar_days_between(block_end.date, instance.date)
                if actual_days_off < days_after:
                    penalty += Decimal(days_after - actual_days_off) * days_after_penalty
    return penalty


def _is_weekend_designated(instance):
    """Use the Shift Builder selection as the only weekend definition."""
    day = getattr(instance, 'date', None)
    template = getattr(instance, 'shift_template', None)
    if day is None or template is None:
        return False
    return day.strftime('%A') in (getattr(template, 'weekend_days', None) or [])


def _published_weekend_lookback_days(contracts):
    """Return enough history to evaluate every configured weekend streak."""
    maximum_weeks = 1
    maximum_shift_streak = 1
    for contract in contracts.values():
        settings = (
            contract.weekend_settings
            if isinstance(getattr(contract, 'weekend_settings', None), dict)
            else {}
        )
        for key in ('min_consecutive_weekends', 'max_consecutive_weekends'):
            maximum_weeks = max(
                maximum_weeks,
                _configured_positive_int(settings, key) or 0,
            )
        for key in (
            'min_consecutive_weekend_shifts',
            'max_consecutive_weekend_shifts',
        ):
            maximum_shift_streak = max(
                maximum_shift_streak,
                _configured_positive_int(settings, key) or 0,
            )
    # One extra calendar week proves where a preceding streak begins. The
    # shift allowance covers nonstandard administrator-designated weekend days.
    return max(14, (maximum_weeks + 1) * 7, maximum_shift_streak + 7)


def _published_boundary_lookback_days(contracts):
    """Return the rule-driven history needed by cross-block sequence rules.

    Period volume rules remain scoped to the new block.  This horizon exists
    only so overlap, rest, and contiguous assignment rules can see far enough
    into the effective live schedule immediately before the block boundary.
    """
    maximum_days = _published_weekend_lookback_days(contracts)
    for contract in contracts.values():
        workload = (
            contract.workload_settings
            if isinstance(getattr(contract, 'workload_settings', None), dict)
            else {}
        )
        night = _night_settings(contract)
        rest_hours = _minimum_rest_hours(contract)
        rest_days = int((rest_hours / Decimal('24')).to_integral_value(rounding=ROUND_CEILING))
        maximum_days = max(
            maximum_days,
            rest_days + 2,
            (_configured_positive_int(workload, 'max_days_in_row') or 0) + 2,
            (_configured_positive_int(night, 'min_consecutive_night_shifts') or 0) + 2,
            (_configured_positive_int(night, 'max_consecutive_night_shifts') or 0) + 2,
            (_configured_positive_int(night, 'days_off_after_night_block') or 0) + 2,
            (_configured_positive_int(night, 'days_off_before_next_night_shift') or 0) + 2,
        )
    return max(maximum_days, 14)


def _attach_published_boundary_context(version, instances, contracts):
    """Attach immutable assignments from the adjacent effective live block.

    Candidate scoring runs thousands of times, so the database lookup is done
    once and the resulting mapping is attached to the in-memory current-block
    instances. Prior assignments inform boundary safety and sequence scoring,
    but never enter optimizer state and therefore cannot be moved or deleted.
    Period totals (monthly/block workload, nights, and weekends) continue to
    count only assignments inside the new block.
    """
    context = defaultdict(list)
    boundary_date_range = None
    if not instances:
        return context

    previous_day = version.schedule_block.start_date - timedelta(days=1)
    previous_block = (
        ScheduleBlock.objects.filter(
            published_at__isnull=False,
            end_date=previous_day,
        )
        .order_by('-published_at', '-id')
        .first()
    )
    if previous_block is not None:
        boundary_date_range = (previous_block.start_date, previous_block.end_date)
        previous_versions = ScheduleVersion.objects.filter(
            schedule_block=previous_block,
            domain=version.domain,
        )
        previous_version = (
            previous_versions.filter(
                optimizer_runs__is_active=True,
                optimizer_runs__status=OptimizerRun.Status.COMPLETED,
            )
            .order_by('-version_number', '-id')
            .first()
            or previous_versions.order_by('-version_number', '-id').first()
        )
        if previous_version is not None:
            previous_run = previous_version.optimizer_runs.filter(
                is_active=True,
                status=OptimizerRun.Status.COMPLETED,
            ).order_by('-run_number').first()
            lookback_start = (
                version.schedule_block.start_date
                - timedelta(days=_published_boundary_lookback_days(contracts))
            )
            prior_assignments = (
                assignments_for_viewed_run(previous_version, previous_run)
                .filter(shift_instance__date__gte=lookback_start)
                .select_related('shift_instance__shift_template')
            )
            for assignment in prior_assignments:
                context[assignment.physician_id].append(assignment.shift_instance)

    frozen_context = {
        physician_id: tuple(sorted(
            prior_instances,
            key=lambda item: (item.date, item.id),
        ))
        for physician_id, prior_instances in context.items()
    }
    weekend_context = {
        physician_id: tuple(
            instance for instance in prior_instances
            if _is_weekend_designated(instance)
        )
        for physician_id, prior_instances in frozen_context.items()
    }
    for instance in instances:
        instance._published_boundary_context = frozen_context
        instance._published_weekend_context = weekend_context
        instance._published_boundary_date_range = boundary_date_range
    return frozen_context


def _attach_published_weekend_context(version, instances, contracts):
    """Backward-compatible entry point for the generalized boundary loader."""
    context = _attach_published_boundary_context(version, instances, contracts)
    return {
        physician_id: tuple(
            instance for instance in prior_instances
            if _is_weekend_designated(instance)
        )
        for physician_id, prior_instances in context.items()
    }


def _published_boundary_context(instances):
    return (
        getattr(instances[0], '_published_boundary_context', {})
        if instances else {}
    )


def _weekend_volume_report(instances, physicians, state, contracts, default_target=None, details=False):
    """Score every configured weekend rule at its documented scope.

    Volume rules are evaluated once per configured calendar month or schedule
    block.  Consecutive-weekend-shift rules are evaluated once per contiguous
    block of weekend-designated shift dates.  Consecutive-weekend rules are
    evaluated once per streak of calendar weekends containing at least one
    weekend-designated assignment.
    """
    assigned = defaultdict(list)
    for instance in instances:
        if _is_weekend_designated(instance):
            for pid in state.get(instance.id, []):
                assigned[pid].append(instance)
    published_context = (
        getattr(instances[0], '_published_weekend_context', {})
        if instances else {}
    )
    boundary_context = _published_boundary_context(instances)
    current_instance_ids = {instance.id for instance in instances}
    if default_target is None:
        default_target = Decimal(sum(map(len, assigned.values()))) / len(physicians) if physicians else Decimal('0')
    score, violations = Decimal('0'), []
    windows = {}
    for physician in physicians:
        contract = contracts.get(physician.id)
        if contract is None:
            continue
        settings = contract.weekend_settings if isinstance(contract.weekend_settings, dict) else {}
        rules = [rule for rule in (settings.get('period_rules') or []) if isinstance(rule, dict)
                 and any(_decimal_or_none(rule.get(key)) is not None for key in ('min_volume', 'max_volume'))]
        for rule in rules:
            period = rule.get('period_type') or 'SCHEDULE_BLOCK'
            if period not in windows:
                windows[period] = _period_windows(instances, period)
            for start, end in windows[period]:
                shifts = [x for x in assigned[physician.id] if start <= x.date <= end]
                count = Decimal(len(shifts))
                for side, kind in (('min', 'WEEKEND_UNDER_MINIMUM'), ('max', 'WEEKEND_OVER_MAXIMUM')):
                    limit = _decimal_or_none(rule.get(f'{side}_volume'))
                    if limit is None:
                        continue
                    excess = max(limit - count if side == 'min' else count - limit, Decimal('0'))
                    weight = _positive_decimal_or_default(
                        rule.get(f'{side}_penalty_weight'), Decimal('0'),
                    )
                    penalty = excess * weight
                    score += penalty
                    if details and penalty:
                        violations.append({
                            'physician_id': physician.id, **_contract_rule_identity(contract),
                            'violation_type': kind, 'period_type': period,
                            'period_start': start.isoformat(), 'period_end': end.isoformat(),
                            'dates_involved': [x.date.isoformat() for x in shifts],
                            'shift_instance_ids': [x.id for x in shifts],
                            'configured_limit': float(limit), 'actual_value': float(count),
                            'penalty_weight': float(weight), 'penalty': float(penalty),
                            'explanation': f"Weekend shifts are {'below the minimum' if side == 'min' else 'above the maximum'} for {start} through {end}.",
                        })

        streak_assignments = [
            *published_context.get(physician.id, ()),
            *assigned[physician.id],
        ]
        weekend_blocks = []
        current_block = []
        previous_date = None
        for instance in sorted(
            streak_assignments,
            key=lambda item: (item.date, item.id),
        ):
            if (
                previous_date is None
                or instance.date == previous_date
                or instance.date == previous_date + timedelta(days=1)
            ):
                current_block.append(instance)
            else:
                weekend_blocks.append(current_block)
                current_block = [instance]
            previous_date = instance.date
        if current_block:
            weekend_blocks.append(current_block)

        for side, kind in (
            ('min', 'MIN_CONSECUTIVE_WEEKEND_SHIFTS'),
            ('max', 'MAX_CONSECUTIVE_WEEKEND_SHIFTS'),
        ):
            limit = _configured_positive_int(
                settings,
                f'{side}_consecutive_weekend_shifts',
            )
            weight = _configured_positive_penalty(
                settings,
                f'{side}_consecutive_weekend_shifts_penalty_weight',
                Decimal('0'),
            )
            if limit is None or weight <= 0:
                continue
            for block in weekend_blocks:
                # Prior published assignments are immutable context. A block
                # wholly outside the current schedule must not be rescored.
                if not any(
                    instance.id in current_instance_ids for instance in block
                ):
                    continue
                actual = len(block)
                if side == 'min':
                    deviation = max(limit - actual, 0)
                else:
                    prior_count = sum(
                        instance.id not in current_instance_ids
                        for instance in block
                    )
                    deviation = max(
                        actual - limit - max(prior_count - limit, 0),
                        0,
                    )
                penalty = Decimal(deviation) * weight
                score += penalty
                if details and penalty:
                    violations.append({
                        'physician_id': physician.id,
                        **_contract_rule_identity(contract),
                        'violation_type': kind,
                        'period_type': 'WEEKEND_BLOCK',
                        'period_start': block[0].date.isoformat(),
                        'period_end': block[-1].date.isoformat(),
                        'dates_involved': [instance.date.isoformat() for instance in block],
                        'shift_instance_ids': [instance.id for instance in block],
                        'configured_limit': limit,
                        'actual_value': actual,
                        'penalty_weight': float(weight),
                        'penalty': float(penalty),
                        'explanation': (
                            f"Weekend shift block has {actual} consecutive shift(s); "
                            f"the configured {'minimum' if side == 'min' else 'maximum'} is {limit}."
                        ),
                    })

        # A weekend is keyed by its Monday-starting calendar week. Multiple
        # assignments during the same weekend count as one worked weekend.
        instances_by_weekend = defaultdict(list)
        for instance in streak_assignments:
            weekend_start = instance.date - timedelta(days=instance.date.weekday())
            instances_by_weekend[weekend_start].append(instance)
        weekend_streaks = []
        current_streak = []
        previous_weekend = None
        for weekend_start in sorted(instances_by_weekend):
            if (
                previous_weekend is None
                or weekend_start == previous_weekend + timedelta(days=7)
            ):
                current_streak.append(weekend_start)
            else:
                weekend_streaks.append(current_streak)
                current_streak = [weekend_start]
            previous_weekend = weekend_start
        if current_streak:
            weekend_streaks.append(current_streak)

        for side, kind in (
            ('min', 'MIN_CONSECUTIVE_WEEKENDS'),
            ('max', 'MAX_CONSECUTIVE_WEEKENDS'),
        ):
            limit = _configured_positive_int(settings, f'{side}_consecutive_weekends')
            weight = _configured_positive_penalty(
                settings,
                f'{side}_consecutive_weekends_penalty_weight',
                Decimal('0'),
            )
            if limit is None or weight <= 0:
                continue
            for streak in weekend_streaks:
                if not any(
                    instance.id in current_instance_ids
                    for weekend_start in streak
                    for instance in instances_by_weekend[weekend_start]
                ):
                    continue
                actual = len(streak)
                if side == 'min':
                    deviation = max(limit - actual, 0)
                else:
                    prior_weekend_count = sum(
                        not any(
                            instance.id in current_instance_ids
                            for instance in instances_by_weekend[weekend_start]
                        )
                        for weekend_start in streak
                    )
                    deviation = max(
                        actual - limit
                        - max(prior_weekend_count - limit, 0),
                        0,
                    )
                penalty = Decimal(deviation) * weight
                score += penalty
                if details and penalty:
                    streak_instances = sorted(
                        (
                            instance
                            for weekend_start in streak
                            for instance in instances_by_weekend[weekend_start]
                        ),
                        key=lambda item: (item.date, item.id),
                    )
                    violations.append({
                        'physician_id': physician.id,
                        **_contract_rule_identity(contract),
                        'violation_type': kind,
                        'period_type': 'WEEKEND_STREAK',
                        'period_start': streak[0].isoformat(),
                        'period_end': (streak[-1] + timedelta(days=6)).isoformat(),
                        'dates_involved': [
                            instance.date.isoformat() for instance in streak_instances
                        ],
                        'shift_instance_ids': [instance.id for instance in streak_instances],
                        'configured_limit': limit,
                        'actual_value': actual,
                        'penalty_weight': float(weight),
                        'penalty': float(penalty),
                        'explanation': (
                            f"Worked-weekend streak contains {actual} consecutive weekend(s); "
                            f"the configured {'minimum' if side == 'min' else 'maximum'} is {limit}."
                        ),
                    })

        friday_rule_enabled = bool(
            settings.get('block_friday_night_before_weekend_off')
        )
        friday_rule_weight = _configured_positive_penalty(
            settings,
            'block_friday_night_before_weekend_off_penalty_weight',
            Decimal('0'),
        )
        if friday_rule_enabled and friday_rule_weight > 0 and instances:
            schedule_block = getattr(instances[0], 'schedule_block', None)
            prior_date_range = getattr(
                instances[0], '_published_boundary_date_range', None,
            )
            block_start = (
                schedule_block.start_date if schedule_block is not None
                else min(instance.date for instance in instances)
            )
            block_end = (
                schedule_block.end_date if schedule_block is not None
                else max(instance.date for instance in instances)
            )
            assigned_instances = [
                *boundary_context.get(physician.id, ()),
                *[
                    instance
                    for instance in instances
                    if physician.id in state.get(instance.id, ())
                ],
            ]
            assigned_by_date = defaultdict(list)
            for instance in assigned_instances:
                assigned_by_date[instance.date].append(instance)
            for friday in sorted(
                day for day in assigned_by_date
                if day.weekday() == 4
            ):
                saturday = friday + timedelta(days=1)
                sunday = friday + timedelta(days=2)
                # Score a split weekend when at least one of its days belongs
                # to this build and every outside day is covered by the
                # adjacent effective live block.  This lets a Sunday-starting
                # block respond to Friday/Saturday history without guessing
                # about an unknown future schedule.
                if sunday < block_start or saturday > block_end:
                    continue
                weekend_dates = (saturday, sunday)
                if any(
                    not (
                        block_start <= day <= block_end
                        or (
                            prior_date_range is not None
                            and prior_date_range[0] <= day <= prior_date_range[1]
                        )
                    )
                    for day in weekend_dates
                ):
                    continue
                friday_nights = [
                    instance for instance in assigned_by_date[friday]
                    if getattr(instance.shift_template, 'night_shift', False)
                ]
                if not friday_nights:
                    continue
                weekend_assignments = [
                    instance
                    for day in (saturday, sunday)
                    for instance in assigned_by_date.get(day, ())
                    if day.strftime('%A') in (
                        instance.shift_template.weekend_days or []
                    )
                ]
                if weekend_assignments:
                    continue
                penalty = friday_rule_weight
                score += penalty
                if details:
                    violations.append({
                        'physician_id': physician.id,
                        **_contract_rule_identity(contract),
                        'violation_type': 'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF',
                        'period_type': 'WEEKEND',
                        'period_start': saturday.isoformat(),
                        'period_end': sunday.isoformat(),
                        'dates_involved': [friday.isoformat()],
                        'shift_instance_ids': [instance.id for instance in friday_nights],
                        'configured_limit': 'No Friday night before weekend off',
                        'actual_value': 'Friday night assigned; Saturday and Sunday off',
                        'penalty_weight': float(friday_rule_weight),
                        'penalty': float(penalty),
                        'explanation': (
                            'A Friday night shift was assigned immediately before a '
                            'complete Saturday-Sunday weekend off.'
                        ),
                    })
    return {'score': score, 'violations': violations}


def _weekend_repair_candidates(
    instances, physicians, state, manual_pairs, contract_by_physician,
):
    """Return movable assignments named by the authoritative weekend report.

    The report already applies each physician's contract settings and each
    shift template's administrator-selected weekend days.  Deriving repair
    targets from it prevents the search from inventing a Saturday/Sunday
    definition that differs from scoring.
    """
    report = _weekend_volume_report(
        instances, physicians, state, contract_by_physician, details=True,
    )
    targets = []
    seen = set()
    for violation in sorted(
        report.get('violations', ()),
        key=lambda row: (
            -Decimal(str(row.get('penalty', 0))),
            row.get('physician_id', 0),
            row.get('period_start') or '',
        ),
    ):
        physician_id = violation.get('physician_id')
        for instance_id in violation.get('shift_instance_ids') or ():
            pair = (physician_id, instance_id)
            if (
                physician_id in state.get(instance_id, ())
                and (instance_id, physician_id) not in manual_pairs
                and pair not in seen
            ):
                seen.add(pair)
                targets.append(pair)
    return targets


def _repair_weekend_support_swaps(
    *, instances, physicians, state, manual_pairs, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
    should_stop=lambda: False, candidate_limit=600, on_improvement=None,
):
    """Join or shorten configured weekend blocks without moving their anchor.

    A minimum-weekend violation is usually not repaired by moving the isolated
    assignment named by the violation: that merely creates another isolated
    assignment elsewhere.  Instead, retain the violating assignment as an
    anchor and exchange one of the physician's other movable assignments for a
    configured weekend shift next to the anchor.  The authoritative complete
    score remains the acceptance test, so this is safe for every combination
    of contract rules and administrator-selected weekend days.
    """
    current = _copy_state(state)
    current_scoring = _score_schedule(
        instances, physicians, current, targets, contract_by_physician,
        requests_by_physician_date, eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    instances_by_id = {instance.id: instance for instance in instances}
    locked_open_instance_ids = {
        instance.id for instance in instances if instance.is_locked_open
    }
    debug = {
        'attempts': 0, 'legal_candidates': 0, 'scored_candidates': 0,
        'accepts': [], 'rescans': 0, 'stopped_reason': 'candidates_exhausted',
    }

    weekend_date_counts_by_physician = {}

    def assigned_weekend_dates(physician_id, exclude_instance_id=None):
        date_counts = weekend_date_counts_by_physician.get(
            physician_id, Counter(),
        )
        excluded_date = None
        if exclude_instance_id is not None:
            excluded_instance = instances_by_id[exclude_instance_id]
            if (
                physician_id in current.get(exclude_instance_id, ())
                and _is_weekend_designated(excluded_instance)
            ):
                excluded_date = excluded_instance.date
        return {
            day
            for day, count in date_counts.items()
            if count - int(day == excluded_date) > 0
        }

    def joins_configured_weekend(day, existing_days):
        if any(abs((day - other_day).days) == 1 for other_day in existing_days):
            return True
        day_week = day - timedelta(days=day.weekday())
        return any(
            abs((day_week - (
                other_day - timedelta(days=other_day.weekday())
            )).days) == 7
            for other_day in existing_days
        )

    while not should_stop() and debug['attempts'] < candidate_limit:
        report = _weekend_volume_report(
            instances, physicians, current, contract_by_physician, details=True,
        )
        anchors = []
        seen = set()
        for violation in sorted(
            report.get('violations', ()),
            key=lambda row: -Decimal(str(row.get('penalty', 0))),
        ):
            physician_id = violation.get('physician_id')
            violation_type = violation.get('violation_type') or ''
            for instance_id in violation.get('shift_instance_ids') or ():
                anchor = (instance_id, physician_id, violation_type)
                if (
                    physician_id in current.get(instance_id, ())
                    and (instance_id, physician_id) not in manual_pairs
                    and anchor not in seen
                ):
                    seen.add(anchor)
                    anchors.append(anchor)
        if not anchors:
            debug['stopped_reason'] = 'no_weekend_targets'
            break

        debug['rescans'] += 1
        movable_pairs = _optimizer_pairs(current, manual_pairs)
        movable_by_physician = defaultdict(list)
        weekend_pairs_by_date = defaultdict(list)
        weekend_pairs_by_week = defaultdict(list)
        for pair in movable_pairs:
            movable_by_physician[pair[1]].append(pair)
            pair_instance = instances_by_id[pair[0]]
            if _is_weekend_designated(pair_instance):
                weekend_pairs_by_date[pair_instance.date].append(pair)
                week_start = pair_instance.date - timedelta(
                    days=pair_instance.date.weekday(),
                )
                weekend_pairs_by_week[week_start].append(pair)
        weekend_date_counts_by_physician = defaultdict(Counter)
        for instance_index, (
            assigned_instance_id, assigned_physician_ids,
        ) in enumerate(current.items()):
            if instance_index % 64 == 0 and should_stop():
                debug['stopped_reason'] = 'runtime_or_candidate_limit'
                break
            assigned_instance = instances_by_id[assigned_instance_id]
            if not _is_weekend_designated(assigned_instance):
                continue
            for assigned_physician_id in assigned_physician_ids:
                weekend_date_counts_by_physician[
                    assigned_physician_id
                ][assigned_instance.date] += 1
        if should_stop():
            debug['stopped_reason'] = 'runtime_or_candidate_limit'
            break

        source_rows = []
        for anchor_instance_id, physician_id, violation_type in anchors:
            if should_stop():
                debug['stopped_reason'] = 'runtime_or_candidate_limit'
                break
            anchor_pair = (anchor_instance_id, physician_id)
            support_pairs = [
                pair for pair in movable_by_physician[physician_id]
                if pair != anchor_pair
            ]
            support_pairs.sort(key=lambda pair: (
                0 if not _is_weekend_designated(instances_by_id[pair[0]]) else 1,
                abs((
                    instances_by_id[pair[0]].date
                    - instances_by_id[anchor_instance_id].date
                ).days),
                pair[0],
            ))
            support_pairs = support_pairs[:20]
            if (
                violation_type.startswith('MIN_')
                or violation_type == 'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF'
            ):
                sources = [*support_pairs, anchor_pair]
            else:
                sources = [anchor_pair, *support_pairs]
            source_rows.extend(
                (anchor_instance_id, violation_type, pair)
                for pair in sources
            )
        source_rows = list(dict.fromkeys(source_rows))

        accepted = False
        debug.setdefault('candidate_sources', 0)
        debug.setdefault('indexed_candidate_pairs', 0)
        for anchor_instance_id, violation_type, left_pair in source_rows:
            if (
                accepted or should_stop()
                or debug['attempts'] >= candidate_limit
            ):
                debug['stopped_reason'] = 'runtime_or_candidate_limit'
                break
            left_instance_id, left_physician_id = left_pair
            anchor_instance = instances_by_id[anchor_instance_id]
            left_instance = instances_by_id[left_instance_id]
            left_remaining = assigned_weekend_dates(
                left_physician_id, left_instance_id,
            )

            # Minimum-weekend repairs only need assignments on a handful of
            # dates adjacent to the reported anchor. Indexing those dates
            # prevents a large block from spending the entire adaptive slice
            # scanning and sorting every movable assignment before evaluating
            # even one candidate.
            if violation_type == 'MIN_CONSECUTIVE_WEEKEND_SHIFTS':
                candidate_pool = [
                    pair
                    for offset in (-1, 1)
                    for pair in weekend_pairs_by_date[
                        anchor_instance.date + timedelta(days=offset)
                    ]
                ]
            elif violation_type == 'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF':
                candidate_pool = [
                    pair
                    for offset in (-2, -1, 1, 2)
                    for pair in weekend_pairs_by_date[
                        anchor_instance.date + timedelta(days=offset)
                    ]
                ]
            elif violation_type == 'MIN_CONSECUTIVE_WEEKENDS':
                anchor_week = anchor_instance.date - timedelta(
                    days=anchor_instance.date.weekday(),
                )
                candidate_pool = [
                    pair
                    for offset in (-7, 7)
                    for pair in weekend_pairs_by_week[
                        anchor_week + timedelta(days=offset)
                    ]
                ]
            else:
                candidate_pool = movable_pairs

            # A physician may have more than one violation naming the same
            # assignment. Avoid duplicate work while preserving deterministic
            # ordering for reproducible seeded runs.
            candidate_pool = list(dict.fromkeys(candidate_pool))
            debug['candidate_sources'] += 1
            debug['indexed_candidate_pairs'] += len(candidate_pool)
            candidates = []
            for candidate_index, (
                right_instance_id, right_physician_id,
            ) in enumerate(candidate_pool):
                if candidate_index % 64 == 0 and should_stop():
                    debug['stopped_reason'] = 'runtime_or_candidate_limit'
                    break
                if (
                    right_instance_id == left_instance_id
                    or right_physician_id == left_physician_id
                ):
                    continue
                right_instance = instances_by_id[right_instance_id]
                day_delta = abs((right_instance.date - anchor_instance.date).days)
                if (
                    violation_type == 'MIN_CONSECUTIVE_WEEKEND_SHIFTS'
                    and (
                        not _is_weekend_designated(right_instance)
                        or day_delta != 1
                    )
                ):
                    continue
                if (
                    violation_type == 'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF'
                    and (
                        not _is_weekend_designated(right_instance)
                        or day_delta not in {1, 2}
                    )
                ):
                    continue
                if (
                    violation_type == 'MIN_CONSECUTIVE_WEEKENDS'
                    and (
                        not _is_weekend_designated(right_instance)
                        or abs((
                            right_instance.date
                            - timedelta(days=right_instance.date.weekday())
                            - (
                                anchor_instance.date
                                - timedelta(days=anchor_instance.date.weekday())
                            )
                        ).days) != 7
                    )
                ):
                    continue
                right_remaining = assigned_weekend_dates(
                    right_physician_id, right_instance_id,
                )
                right_joins_left = (
                    _is_weekend_designated(right_instance)
                    and joins_configured_weekend(
                        right_instance.date,
                        left_remaining | {anchor_instance.date},
                    )
                )
                left_joins_right = (
                    _is_weekend_designated(left_instance)
                    and joins_configured_weekend(
                        left_instance.date, right_remaining,
                    )
                )
                candidates.append((
                    (
                        -(int(right_joins_left) + int(left_joins_right)),
                        0 if day_delta == 1 else 1,
                        0 if right_joins_left else 1,
                        0 if _is_weekend_designated(right_instance) else 1,
                        abs(_shift_hours(right_instance) - _shift_hours(left_instance)),
                        day_delta, right_physician_id, right_instance_id,
                    ),
                    (right_instance_id, right_physician_id),
                ))
            if should_stop():
                break
            # Evaluate immediately rather than first materializing candidates
            # for every source. A small batch per source preserves breadth and
            # guarantees real score evaluations inside short adaptive slices.
            source_candidate_limit = min(
                8, candidate_limit - debug['attempts'],
            )
            for _priority, right_pair in nsmallest(
                source_candidate_limit, candidates, key=lambda row: row[0],
            ):
                if should_stop() or debug['attempts'] >= candidate_limit:
                    debug['stopped_reason'] = 'runtime_or_candidate_limit'
                    break
                debug['attempts'] += 1
                result = evaluate_plateau_pairwise_swap(
                    instances=instances, physicians=physicians, state=current,
                    instances_by_id=instances_by_id, manual_pairs=manual_pairs,
                    locked_open_instance_ids=locked_open_instance_ids,
                    targets=targets,
                    contract_by_physician=contract_by_physician,
                    requests_by_physician_date=requests_by_physician_date,
                    eligible_facilities_by_physician=eligible_facilities_by_physician,
                    minimum_rest_by_physician=minimum_rest_by_physician,
                    current_score=current_scoring['score'],
                    left_instance_id=left_pair[0],
                    left_physician_id=left_pair[1],
                    right_instance_id=right_pair[0],
                    right_physician_id=right_pair[1],
                )
                if not result.get('legal'):
                    continue
                debug['legal_candidates'] += 1
                if result.get('scoring') is not None:
                    debug['scored_candidates'] += 1
                if not result.get('improving'):
                    continue
                before = current_scoring['score']
                current = result['state']
                current_scoring = result['scoring']
                accepted = True
                detail = {
                    'weekend_anchor_shift_instance_id': anchor_instance_id,
                    'weekend_violation_type': violation_type,
                    'left_shift_instance_id': left_pair[0],
                    'left_physician_id': left_pair[1],
                    'right_shift_instance_id': right_pair[0],
                    'right_physician_id': right_pair[1],
                    'score_before': float(before),
                    'score_after': float(current_scoring['score']),
                }
                debug['accepts'].append(detail)
                if on_improvement:
                    on_improvement(current, current_scoring)
                break
        if not accepted:
            break
    return current, current_scoring, debug


def _repair_weekend_support_cycles(
    *, instances, physicians, state, manual_pairs, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
    should_stop=lambda: False, candidate_limit=250, on_improvement=None,
):
    """Repair weekend blocks with a bounded three-person assignment cycle.

    A direct swap can be blocked when the physician receiving the displaced
    weekday shift cannot work it, even though a third physician can. This
    operator preserves every physician's assignment count while rotating the
    three shifts. It is intentionally activated only after a complete ordinary
    repair epoch fails to improve the global best.
    """
    current = _copy_state(state)
    current_scoring = _score_schedule(
        instances, physicians, current, targets, contract_by_physician,
        requests_by_physician_date, eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    instances_by_id = {instance.id: instance for instance in instances}
    locked_open_instance_ids = {
        instance.id for instance in instances if instance.is_locked_open
    }
    debug = {
        'attempts': 0, 'legal_candidates': 0, 'scored_candidates': 0,
        'accepts': [], 'rescans': 0, 'stopped_reason': 'candidates_exhausted',
    }

    while not should_stop() and debug['attempts'] < candidate_limit:
        report = _weekend_volume_report(
            instances, physicians, current, contract_by_physician, details=True,
        )
        anchors = []
        seen_anchors = set()
        for violation in sorted(
            report.get('violations', ()),
            key=lambda row: -Decimal(str(row.get('penalty', 0))),
        ):
            violation_type = violation.get('violation_type') or ''
            if violation_type not in {
                'MIN_CONSECUTIVE_WEEKEND_SHIFTS',
                'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF',
            }:
                continue
            physician_id = violation.get('physician_id')
            for instance_id in violation.get('shift_instance_ids') or ():
                anchor = (instance_id, physician_id, violation_type)
                if (
                    physician_id in current.get(instance_id, ())
                    and anchor not in seen_anchors
                ):
                    seen_anchors.add(anchor)
                    anchors.append(anchor)
        if not anchors:
            debug['stopped_reason'] = 'no_weekend_targets'
            break

        debug['rescans'] += 1
        movable_pairs = _optimizer_pairs(current, manual_pairs)
        movable_by_physician = defaultdict(list)
        weekend_pairs_by_date = defaultdict(list)
        for pair in movable_pairs:
            movable_by_physician[pair[1]].append(pair)
            pair_instance = instances_by_id[pair[0]]
            if _is_weekend_designated(pair_instance):
                weekend_pairs_by_date[pair_instance.date].append(pair)

        accepted = False
        for anchor_instance_id, anchor_physician_id, violation_type in anchors[:16]:
            if should_stop() or debug['attempts'] >= candidate_limit:
                break
            anchor_instance = instances_by_id[anchor_instance_id]
            offsets = (
                (-1, 1)
                if violation_type == 'MIN_CONSECUTIVE_WEEKEND_SHIFTS'
                else (-2, -1, 1, 2)
            )
            adjacent_pairs = list(dict.fromkeys(
                pair
                for offset in offsets
                for pair in weekend_pairs_by_date[
                    anchor_instance.date + timedelta(days=offset)
                ]
                if pair[1] != anchor_physician_id
            ))
            adjacent_pairs = nsmallest(
                6,
                adjacent_pairs,
                key=lambda pair: (
                    abs((instances_by_id[pair[0]].date - anchor_instance.date).days),
                    abs(_shift_hours(instances_by_id[pair[0]]) - _shift_hours(anchor_instance)),
                    pair,
                ),
            )
            support_pairs = nsmallest(
                8,
                (
                    pair for pair in movable_by_physician[anchor_physician_id]
                    if pair[0] != anchor_instance_id
                ),
                key=lambda pair: (
                    0 if not _is_weekend_designated(instances_by_id[pair[0]]) else 1,
                    abs((instances_by_id[pair[0]].date - anchor_instance.date).days),
                    pair[0],
                ),
            )
            for adjacent_pair in adjacent_pairs:
                adjacent_owner = adjacent_pair[1]
                for support_pair in support_pairs:
                    support_instance = instances_by_id[support_pair[0]]
                    bridge_pairs = nsmallest(
                        10,
                        (
                            pair for pair in movable_pairs
                            if pair[0] not in {
                                adjacent_pair[0], support_pair[0], anchor_instance_id,
                            }
                            and pair[1] not in {
                                anchor_physician_id, adjacent_owner,
                            }
                        ),
                        key=lambda pair: (
                            abs(
                                _shift_hours(instances_by_id[pair[0]])
                                - _shift_hours(support_instance)
                            ),
                            abs((
                                instances_by_id[pair[0]].date
                                - support_instance.date
                            ).days),
                            pair,
                        ),
                    )
                    for bridge_pair in bridge_pairs:
                        if should_stop() or debug['attempts'] >= candidate_limit:
                            break
                        debug['attempts'] += 1
                        result = evaluate_plateau_three_way_rotation(
                            instances=instances, physicians=physicians,
                            state=current, instances_by_id=instances_by_id,
                            manual_pairs=manual_pairs,
                            locked_open_instance_ids=locked_open_instance_ids,
                            targets=targets,
                            contract_by_physician=contract_by_physician,
                            requests_by_physician_date=requests_by_physician_date,
                            eligible_facilities_by_physician=eligible_facilities_by_physician,
                            minimum_rest_by_physician=minimum_rest_by_physician,
                            current_score=current_scoring['score'],
                            assignment_pairs=(
                                adjacent_pair, support_pair, bridge_pair,
                            ),
                            new_physician_ids=(
                                anchor_physician_id,
                                bridge_pair[1],
                                adjacent_owner,
                            ),
                        )
                        if not result.get('legal'):
                            continue
                        debug['legal_candidates'] += 1
                        if result.get('scoring') is not None:
                            debug['scored_candidates'] += 1
                        if not result.get('improving'):
                            continue
                        before = current_scoring['score']
                        current = result['state']
                        current_scoring = result['scoring']
                        accepted = True
                        debug['accepts'].append({
                            'weekend_anchor_shift_instance_id': anchor_instance_id,
                            'weekend_violation_type': violation_type,
                            'assignment_pairs': [list(pair) for pair in (
                                adjacent_pair, support_pair, bridge_pair,
                            )],
                            'score_before': float(before),
                            'score_after': float(current_scoring['score']),
                        })
                        if on_improvement:
                            on_improvement(current, current_scoring)
                        break
                    if accepted or should_stop() or debug['attempts'] >= candidate_limit:
                        break
                if accepted or should_stop() or debug['attempts'] >= candidate_limit:
                    break
            if accepted:
                break
        if not accepted:
            if should_stop():
                debug['stopped_reason'] = 'runtime_or_candidate_limit'
            elif debug['attempts'] >= candidate_limit:
                debug['stopped_reason'] = 'candidate_budget'
            break
    return current, current_scoring, debug


def _shift_period_report(instances, physicians, state, contracts, details=False):
    """Score contract shift-group rules in their configured period windows."""
    assigned_by_physician = defaultdict(list)
    for instance in instances:
        for physician_id in state.get(instance.id, ()):
            assigned_by_physician[physician_id].append(instance)

    score = Decimal('0')
    violations = []
    windows = {}
    for physician in physicians:
        contract = contracts.get(physician.id)
        if contract is None:
            continue
        raw_settings = getattr(contract, 'shift_settings', {})
        settings = raw_settings if isinstance(raw_settings, dict) else {}
        for group_index, group in enumerate(settings.get('rules') or []):
            if not isinstance(group, dict):
                continue
            template_ids = {
                int(template_id)
                for template_id in (group.get('shift_template_ids') or [])
                if str(template_id).isdigit()
            }
            if not template_ids:
                continue
            group_label = group.get('label') or f'Shift rule group {group_index + 1}'
            for rule in group.get('period_rules') or []:
                if not isinstance(rule, dict):
                    continue
                minimum = _decimal_or_none(rule.get('min_value'))
                maximum = _decimal_or_none(rule.get('max_value'))
                if minimum is None and maximum is None:
                    continue
                period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
                if period_type not in {'WEEK', 'MONTH', 'SCHEDULE_BLOCK'}:
                    period_type = 'SCHEDULE_BLOCK'
                if period_type not in windows:
                    windows[period_type] = _period_windows(instances, period_type)
                units = 'SHIFTS' if rule.get('units') == 'SHIFTS' else 'HOURS'
                normalized = {
                    'period_type': period_type,
                    'units': units,
                    'min_value': minimum,
                    'max_value': maximum,
                    'min_penalty_weight': _positive_decimal_or_default(
                        rule.get('min_penalty_weight'), Decimal('0'),
                    ),
                    'max_penalty_weight': _positive_decimal_or_default(
                        rule.get('max_penalty_weight'), Decimal('0'),
                    ),
                }
                for window_start, window_end in windows[period_type]:
                    effective = _effective_workload_rule(
                        normalized, window_start, window_end,
                    )
                    matching = [
                        instance
                        for instance in assigned_by_physician[physician.id]
                        if instance.shift_template_id in template_ids
                        and window_start <= instance.date <= window_end
                    ]
                    actual = (
                        Decimal(len(matching))
                        if units == 'SHIFTS'
                        else sum((_shift_hours(instance) for instance in matching), Decimal('0'))
                    )
                    for side, kind in (
                        ('min', 'SHIFT_GROUP_UNDER_MINIMUM'),
                        ('max', 'SHIFT_GROUP_OVER_MAXIMUM'),
                    ):
                        limit = effective[f'{side}_value']
                        if limit is None:
                            continue
                        deviation = max(
                            limit - actual if side == 'min' else actual - limit,
                            Decimal('0'),
                        )
                        weight = effective[f'{side}_penalty_weight']
                        penalty = deviation * weight
                        score += penalty
                        if details and penalty:
                            violations.append({
                                'physician_id': physician.id,
                                **_contract_rule_identity(contract),
                                'violation_type': kind,
                                'period_type': period_type,
                                'period_start': window_start.isoformat(),
                                'period_end': window_end.isoformat(),
                                'dates_involved': [
                                    instance.date.isoformat() for instance in matching
                                ],
                                'shift_instance_ids': [instance.id for instance in matching],
                                'shift_template_ids': sorted(template_ids),
                                'shift_rule_label': group_label,
                                'configured_limit': float(limit),
                                'actual_value': float(actual),
                                'units': units,
                                'penalty_weight': float(weight),
                                'penalty': float(penalty),
                                'proration': effective['proration'],
                                'explanation': (
                                    f"{group_label} {units.lower()} are "
                                    f"{'below the minimum' if side == 'min' else 'above the maximum'} "
                                    f"for {window_start} through {window_end}."
                                ),
                            })
    return {'score': score, 'violations': violations}


def _template_occurrence_positions(instances):
    positions = {}
    for shift_template_id, template_instances in _instances_by_template(instances).items():
        for index, instance in enumerate(template_instances):
            positions[instance.id] = (shift_template_id, index)
    return positions


def _instances_by_template(instances):
    grouped = defaultdict(list)
    for instance in instances:
        grouped[instance.shift_template_id].append(instance)
    return {
        shift_template_id: sorted(
            template_instances,
            key=lambda item: (item.date, item.start_datetime, item.id),
        )
        for shift_template_id, template_instances in grouped.items()
    }


def _same_shift_streak_score(occurrence_indexes, max_streak, penalty):
    if not occurrence_indexes:
        return Decimal('0'), []
    score = Decimal('0')
    streaks = []
    streak = [occurrence_indexes[0]]
    previous_index = occurrence_indexes[0]
    for occurrence_index in occurrence_indexes[1:]:
        if occurrence_index == previous_index:
            continue
        if occurrence_index == previous_index + 1:
            streak.append(occurrence_index)
        else:
            if len(streak) > max_streak:
                excess = len(streak) - max_streak
                score += Decimal(excess) * penalty
                streaks.append((list(streak), excess))
            streak = [occurrence_index]
        previous_index = occurrence_index
    if len(streak) > max_streak:
        excess = len(streak) - max_streak
        score += Decimal(excess) * penalty
        streaks.append((list(streak), excess))
    return score, streaks


def _same_shift_violation_report(instances, physicians, state, contract_by_physician):
    physicians_by_id = {physician.id: physician for physician in physicians}
    instances_by_template = _instances_by_template(instances)
    occurrence_positions = {
        instance.id: (shift_template_id, index)
        for shift_template_id, template_instances in instances_by_template.items()
        for index, instance in enumerate(template_instances)
    }
    occurrence_indexes_by_physician_template = defaultdict(list)
    for instance_id, physician_ids in state.items():
        if instance_id not in occurrence_positions:
            continue
        shift_template_id, occurrence_index = occurrence_positions[instance_id]
        for physician_id in physician_ids:
            occurrence_indexes_by_physician_template[
                (physician_id, shift_template_id)
            ].append(occurrence_index)

    score = Decimal('0')
    violations = []
    for (physician_id, shift_template_id), occurrence_indexes in occurrence_indexes_by_physician_template.items():
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        same_shift_rule = _same_shift_rule(contract)
        if same_shift_rule is None:
            continue
        max_streak, penalty = same_shift_rule
        streak_score, streaks = _same_shift_streak_score(
            sorted(occurrence_indexes),
            max_streak,
            penalty,
        )
        score += streak_score
        template_instances = instances_by_template[shift_template_id]
        physician = physicians_by_id.get(physician_id)
        for streak_indexes, excess_count in streaks:
            streak_instances = [
                template_instances[index]
                for index in streak_indexes
                if index < len(template_instances)
            ]
            if not streak_instances:
                continue
            violations.append(
                {
                    'physician_id': physician_id,
                    'physician': _physician_display_name(physician) if physician else str(physician_id),
                    'shift_template_id': shift_template_id,
                    'shift_template': streak_instances[0].shift_template.generated_name(),
                    'shift_instance_ids': [
                        instance.id
                        for instance in streak_instances
                    ],
                    'dates_involved': [
                        instance.date.isoformat()
                        for instance in streak_instances
                    ],
                    'excess_count': excess_count,
                    'configured_limit': max_streak,
                    'actual_value': len(streak_instances),
                    'penalty_weight': float(penalty),
                    'penalty': float(Decimal(excess_count) * penalty),
                }
            )
    return score, violations


def _same_shift_candidate_delta(instances, physicians, state, contract_by_physician, physician_id, instance):
    contract = contract_by_physician.get(physician_id)
    if contract is None:
        return Decimal('0')
    positions = _template_occurrence_positions(instances)
    current_position = positions.get(instance.id)
    if current_position is None:
        return Decimal('0')
    shift_template_id, occurrence_index = current_position
    current_indexes = []
    for existing_instance_id, physician_ids in state.items():
        if physician_id not in physician_ids:
            continue
        existing_position = positions.get(existing_instance_id)
        if existing_position is None:
            continue
        existing_template_id, existing_occurrence_index = existing_position
        if existing_template_id == shift_template_id:
            current_indexes.append(existing_occurrence_index)
    same_shift_rule = _same_shift_rule(contract)
    if same_shift_rule is None:
        return Decimal('0')
    max_streak, penalty = same_shift_rule
    current_score, _streaks = _same_shift_streak_score(
        sorted(current_indexes),
        max_streak,
        penalty,
    )
    projected_score, _streaks = _same_shift_streak_score(
        sorted(current_indexes + [occurrence_index]),
        max_streak,
        penalty,
    )
    return projected_score - current_score


def _same_shift_candidate_delta_from_indexes(
    contract, positions, indexes_by_physician_template, physician_id, instance,
):
    """Calculate same-shift delta without rebuilding occurrence positions or assignment history."""
    current_position = positions.get(instance.id)
    if contract is None or current_position is None:
        return Decimal('0')
    shift_template_id, occurrence_index = current_position
    current_indexes = indexes_by_physician_template.get(
        (physician_id, shift_template_id),
        [],
    )
    same_shift_rule = _same_shift_rule(contract)
    if same_shift_rule is None:
        return Decimal('0')
    max_streak, penalty = same_shift_rule
    current_score, _streaks = _same_shift_streak_score(
        sorted(current_indexes), max_streak, penalty,
    )
    projected_score, _streaks = _same_shift_streak_score(
        sorted([*current_indexes, occurrence_index]), max_streak, penalty,
    )
    return projected_score - current_score


def _distribution_score(
    instances,
    physicians,
    state,
    contract_by_physician,
    eligible_facilities_by_physician,
    include_internal_night_heuristics=False,
    default_weekend_target_override=None,
    default_night_target_override=None,
):
    instances_by_id = {instance.id: instance for instance in instances}
    assignments_by_physician = defaultdict(list)
    weekend_counts = defaultdict(int)
    for instance_id, physician_ids in state.items():
        instance = instances_by_id.get(instance_id)
        if instance is None:
            continue
        for physician_id in physician_ids:
            assignments_by_physician[physician_id].append(instance)
            if instance.date.strftime('%A') in (instance.shift_template.weekend_days or []):
                weekend_counts[physician_id] += 1

    consecutive_days_score = Decimal('0')
    facility_distribution_score = Decimal('0')
    total_weekend_assignments = sum(weekend_counts.values())
    default_weekend_target = default_weekend_target_override if default_weekend_target_override is not None else (
        Decimal(total_weekend_assignments) / Decimal(len(physicians))
        if physicians
        else Decimal('0')
    )

    for physician in physicians:
        physician_instances = assignments_by_physician[physician.id]
        contract = contract_by_physician.get(physician.id)
        if contract is None:
            continue

        workload_settings = (
            contract.workload_settings
            if isinstance(contract.workload_settings, dict)
            else {}
        )
        max_days = _decimal_or_none(workload_settings.get('max_days_in_row'))
        max_days_penalty = _decimal_or_none(
            workload_settings.get('max_days_in_row_penalty_weight')
        )
        if (
            max_days is not None and max_days > 0
            and max_days_penalty is not None and max_days_penalty > 0
        ):
            current_dates = {instance.date for instance in physician_instances}
            prior_dates = {
                instance.date
                for instance in _published_boundary_context(instances).get(
                    physician.id, ()
                )
            }
            # Charge only the incremental excess introduced inside this block.
            # A prior-block excess is context, not a debt transferred forward.
            combined_score = _streak_excess_score(
                sorted(current_dates | prior_dates),
                max(int(max_days), 1),
                max_days_penalty,
            )
            prior_score = _streak_excess_score(
                sorted(prior_dates),
                max(int(max_days), 1),
                max_days_penalty,
            )
            consecutive_days_score += max(
                combined_score - prior_score,
                Decimal('0'),
            )

        facility_counts = defaultdict(int)
        for instance in physician_instances:
            facility_counts[instance.facility_id] += 1


    same_shift_score, _violations = _same_shift_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
    )
    # Only contract-configured penalties belong in the authoritative objective.
    # Internal night-clustering guidance may order construction candidates, but
    # including it here makes persisted assignments score differently when a
    # continuation run reloads them.  _night_candidate_delta retains that
    # guidance without allowing it to alter global score acceptance.
    night_report = _night_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
        include_internal_heuristics=False,
        default_target_override=default_night_target_override,
    )
    return {
        'consecutive_days_score': consecutive_days_score,
        'same_shift_score': same_shift_score,
        'shift_rule_score': _shift_period_report(
            instances, physicians, state, contract_by_physician,
        )['score'],
        'night_score': night_report['score'],
        'weekend_score': _weekend_volume_report(
            instances, physicians, state, contract_by_physician, default_weekend_target,
        )['score'],
        'facility_distribution_score': facility_distribution_score,
    }


def _score_schedule(
    instances,
    physicians,
    state,
    targets,
    contract_by_physician,
    requests_by_physician_date,
    eligible_facilities_by_physician,
    minimum_rest_by_physician,
    include_internal_night_heuristics=False,
):
    score_cache = _SCORE_CACHE.get()
    cache_key = None
    if score_cache is not None:
        cache_key = _score_cache_key(
            state,
            include_internal_night_heuristics=include_internal_night_heuristics,
        )
        cached = score_cache.get(cache_key)
        if cached is not None:
            score_cache.move_to_end(cache_key)
            _SCORE_CACHE_HITS.set(_SCORE_CACHE_HITS.get() + 1)
            return cached
        _SCORE_CACHE_MISSES.set(_SCORE_CACHE_MISSES.get() + 1)
    _FULL_SCORE_EVALUATIONS.set(_FULL_SCORE_EVALUATIONS.get() + 1)
    manual_assignment_only_physician_ids = {
        physician_id
        for physician_id, contract in contract_by_physician.items()
        if getattr(contract, 'manual_assignment_only', False)
    }


    scoring_physicians = [
        physician for physician in physicians
        if physician.id not in manual_assignment_only_physician_ids
    ]
    scoring_contract_by_physician = {
        physician_id: contract
        for physician_id, contract in contract_by_physician.items()
        if physician_id not in manual_assignment_only_physician_ids
    }
    instance_by_id = {instance.id: instance for instance in instances}
    physician_hours = defaultdict(lambda: Decimal('0'))
    physician_shifts = defaultdict(int)
    physician_night_shifts = defaultdict(int)
    coverage_score = Decimal('0')
    workload_score = Decimal('0')
    request_score_total = Decimal('0')
    request_violations = 0
    request_rewards = 0
    fixed_request_on_unmet = 0

    for instance in instances:
        assigned_physician_ids = state[instance.id]
        coverage_score += Decimal(
            max(instance.required_staffing - len(assigned_physician_ids), 0)
        ) * Decimal(COVERAGE_PENALTY)
        for physician_id in assigned_physician_ids:
            if physician_id in manual_assignment_only_physician_ids:
                continue
            physician_hours[physician_id] += _shift_hours(instance)
            physician_shifts[physician_id] += 1
            if instance.shift_template.night_shift:
                physician_night_shifts[physician_id] += 1
            contract = contract_by_physician.get(physician_id)
            if contract is None:
                continue
            request_score, violations, rewards = _request_score(
                _requests_for_shift(
                    requests_by_physician_date,
                    physician_id,
                    instance,
                ),
                contract,
            )
            request_score_total += request_score
            request_violations += violations
            request_rewards += rewards

    workload_score_rows = _workload_score_rows(
        scoring_physicians,
        instances,
        state,
        physician_hours,
        physician_shifts,
        physician_night_shifts,
        targets,
    )
    workload_score = sum(
        (
            Decimal(row['score_contribution_exact'])
            for row in workload_score_rows
        ),
        Decimal('0'),
    )
    # There is no implicit "must use every physician" contract rule.
    underutilization_score = Decimal('0')

    for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
        if physician_id in manual_assignment_only_physician_ids:
            continue
        physician_instance_ids = [
            instance_id
            for instance_id, physician_ids in state.items()
            if physician_id in physician_ids
        ]
        physician_instances = [
            instance_by_id[instance_id]
            for instance_id in physician_instance_ids
            if instance_id in instance_by_id
        ]
        for schedule_request in schedule_requests:
            if schedule_request.request_type == ScheduleRequest.RequestType.DAY_ON:
                request_satisfied = any(
                    instance.date == request_date for instance in physician_instances
                )
                if not request_satisfied:
                    contract = contract_by_physician.get(physician_id)
                    if contract is not None:
                        request_score_total += _request_weight(contract, schedule_request.weight)
                        request_violations += 1
                        if schedule_request.weight == ScheduleRequest.Weight.FIXED:
                            fixed_request_on_unmet += 1
                else:
                    request_rewards += 1
            elif schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON:
                template_ids = {template.id for template in schedule_request.shift_templates.all()}
                request_satisfied = any(
                    instance.date == request_date
                    and instance.shift_template_id in template_ids
                    for instance in physician_instances
                )
                if not request_satisfied:
                    contract = contract_by_physician.get(physician_id)
                    if contract is not None:
                        request_score_total += _request_weight(contract, schedule_request.weight)
                        request_violations += 1
                        if schedule_request.weight == ScheduleRequest.Weight.FIXED:
                            fixed_request_on_unmet += 1
                else:
                    request_rewards += 1

    validation = _validate_schedule(
        instances,
        physicians,
        state,
        eligible_facilities_by_physician,
        minimum_rest_by_physician,
        manual_assignment_only_physician_ids,
    )
    rest_score = Decimal(validation['final_rest_violations']) * Decimal(REST_VIOLATION_PENALTY)
    overlap_score = Decimal(validation['final_overlap_violations']) * Decimal(OVERLAP_VIOLATION_PENALTY)
    invalid_assignment_score = (
        Decimal(validation['final_duplicate_violations']) * Decimal(OVERLAP_VIOLATION_PENALTY)
        + Decimal(validation['final_overstaffed_violations']) * Decimal(COVERAGE_PENALTY)
        + Decimal(validation['final_facility_ineligible_violations']) * Decimal(FACILITY_ELIGIBILITY_PENALTY)
        + Decimal(validation['final_inactive_physician_violations']) * Decimal(INACTIVE_PHYSICIAN_PENALTY)
    )
    distribution_scores = _distribution_score(
        instances,
        scoring_physicians,
        state,
        scoring_contract_by_physician,
        eligible_facilities_by_physician,
        include_internal_night_heuristics=include_internal_night_heuristics,
    )
    _same_shift_score, same_shift_violations = _same_shift_violation_report(
        instances,
        scoring_physicians,
        state,
        scoring_contract_by_physician,
    )
    score = (
        coverage_score
        + workload_score
        + request_score_total
        + underutilization_score
        + rest_score
        + overlap_score
        + invalid_assignment_score
        + sum(distribution_scores.values(), Decimal('0'))
    )

    result = {
        'score': score,
        'breakdown': {
            'coverage_score': coverage_score,
            'workload_score': workload_score,
            'underutilization_score': underutilization_score,
            'request_score': request_score_total,
            'rest_score': rest_score,
            'overlap_score': overlap_score,
            'invalid_assignment_score': invalid_assignment_score,
            **distribution_scores,
            'total_score': score,
        },
        'request_violations': request_violations,
        'fixed_request_on_unmet': fixed_request_on_unmet,
        'request_rewards': request_rewards,
        'physician_hours': physician_hours,
        'physician_shifts': physician_shifts,
        'physician_night_shifts': physician_night_shifts,
        'workload_score_rows': workload_score_rows,
        'validation': validation,
        'same_shift_violations': same_shift_violations,
    }
    if score_cache is not None:
        score_cache[cache_key] = result
        score_cache.move_to_end(cache_key)
        if len(score_cache) > SCORE_CACHE_MAX_ENTRIES:
            score_cache.popitem(last=False)
            _SCORE_CACHE_EVICTIONS.set(_SCORE_CACHE_EVICTIONS.get() + 1)
    return result


def _fixed_shift_on_request_workload_floor(
    instances, physicians, targets, requests_by_physician_date,
    eligible_facilities_by_physician, state=None,
    manual_assignment_only_physician_ids=None,
):
    """Prove workload penalties forced by exact Fixed Shift On requests.

    The result is deliberately conservative. A request contributes only when
    its date/template selection resolves to one eligible shift instance. Any
    ambiguous or missing request is reported but is not used in the floor.
    """
    physician_by_id = {physician.id: physician for physician in physicians}
    manual_assignment_only_physician_ids = set(
        manual_assignment_only_physician_ids or ()
    )
    instances_by_date_template = defaultdict(list)
    instances_by_id = {}
    for instance in instances:
        instances_by_id[instance.id] = instance
        instances_by_date_template[(instance.date, instance.shift_template_id)].append(instance)

    forced_instance_requests = defaultdict(lambda: defaultdict(set))
    resolved_requests = []
    unresolved_requests = []
    fixed_request_count = 0
    for (physician_id, request_date), requests in requests_by_physician_date.items():
        for schedule_request in requests:
            if not (
                schedule_request.weight == ScheduleRequest.Weight.FIXED
                and schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON
            ):
                continue
            fixed_request_count += 1
            template_ids = {
                template.id for template in schedule_request.shift_templates.all()
            }
            candidates = []
            for template_id in template_ids:
                candidates.extend(instances_by_date_template.get((request_date, template_id), ()))
            eligible_facility_ids = eligible_facilities_by_physician.get(physician_id)
            # A Shift On request is the assignment authority for a manual-only
            # physician. Their optimizer eligibility is deliberately empty so
            # search cannot add work, but that must not make a real shift
            # instance disappear from request diagnostics.
            if (
                physician_id not in manual_assignment_only_physician_ids
                and eligible_facility_ids is not None
            ):
                candidates = [
                    instance for instance in candidates
                    if instance.facility_id in eligible_facility_ids
                ]
            candidates = list({instance.id: instance for instance in candidates}.values())
            request_payload = {
                'request_id': schedule_request.id,
                'physician_id': physician_id,
                'date': request_date.isoformat(),
                'shift_template_ids': sorted(template_ids),
            }
            if len(candidates) != 1:
                unresolved_requests.append({
                    **request_payload,
                    'reason': 'no_matching_shift_instance' if not candidates else 'ambiguous_matching_shift_instances',
                    'candidate_shift_instance_ids': sorted(instance.id for instance in candidates),
                })
                continue
            instance = candidates[0]
            forced_instance_requests[physician_id][instance.id].add(schedule_request.id)
            resolved_requests.append({
                **request_payload,
                'shift_instance_id': instance.id,
                'facility_id': instance.facility_id,
                'hours': float(_shift_hours(instance)),
                'satisfied_in_viewed_schedule': (
                    physician_id in state.get(instance.id, ()) if state is not None else None
                ),
            })

    assessment_rows = []
    floor_rows = []
    score_floor = Decimal('0')
    for physician_id, forced_by_instance in forced_instance_requests.items():
        target = targets.get(physician_id) or {}
        forced_instances = [
            instances_by_id[instance_id]
            for instance_id in forced_by_instance
            if instance_id in instances_by_id
        ]
        for rule in target.get('rules') or ():
            for window_start, window_end in _period_windows(instances, rule['period_type']):
                effective_rule = _effective_workload_rule(rule, window_start, window_end)
                maximum = effective_rule['max_value']
                if maximum is None:
                    continue
                window_instances = [
                    instance for instance in forced_instances
                    if window_start <= instance.date <= window_end
                ]
                if not window_instances:
                    continue
                required_value = (
                    Decimal(len(window_instances))
                    if effective_rule['units'] == 'SHIFTS'
                    else sum((_shift_hours(instance) for instance in window_instances), Decimal('0'))
                )
                excess = max(required_value - maximum, Decimal('0'))
                penalty = excess * effective_rule['max_penalty_weight']
                assigned_instances = (
                    [
                        instance for instance in instances
                        if window_start <= instance.date <= window_end
                        and physician_id in state.get(instance.id, ())
                    ]
                    if state is not None else []
                )
                actual_value = (
                    Decimal(len(assigned_instances))
                    if state is not None and effective_rule['units'] == 'SHIFTS'
                    else sum((_shift_hours(instance) for instance in assigned_instances), Decimal('0'))
                    if state is not None else None
                )
                actual_excess = (
                    max(actual_value - maximum, Decimal('0'))
                    if actual_value is not None else None
                )
                actual_penalty = (
                    actual_excess * effective_rule['max_penalty_weight']
                    if actual_excess is not None else None
                )
                physician = physician_by_id.get(physician_id)
                row = {
                    'physician_id': physician_id,
                    'physician': _physician_display_name(physician) if physician is not None else str(physician_id),
                    'period_type': rule['period_type'],
                    'period_start': window_start.isoformat(),
                    'period_end': window_end.isoformat(),
                    'units': effective_rule['units'],
                    'fixed_request_required_value': float(required_value),
                    'effective_max_value': float(maximum),
                    'unavoidable_excess': float(excess),
                    'penalty_weight': float(effective_rule['max_penalty_weight']),
                    'score_floor_contribution': float(penalty),
                    'actual_assigned_value': float(actual_value) if actual_value is not None else None,
                    'actual_excess': float(actual_excess) if actual_excess is not None else None,
                    'actual_penalty': float(actual_penalty) if actual_penalty is not None else None,
                    'potentially_avoidable_penalty': (
                        float(max(actual_penalty - penalty, Decimal('0')))
                        if actual_penalty is not None else None
                    ),
                    'shift_instance_ids': sorted(instance.id for instance in window_instances),
                    'request_ids': sorted({
                        request_id
                        for instance in window_instances
                        for request_id in forced_by_instance[instance.id]
                    }),
                }
                assessment_rows.append(row)
                if penalty > 0:
                    floor_rows.append(row)
                score_floor += penalty

    satisfied = [
        row for row in resolved_requests
        if row['satisfied_in_viewed_schedule'] is True
    ]
    unsatisfied = [
        row for row in resolved_requests
        if row['satisfied_in_viewed_schedule'] is False
    ]
    return {
        'fixed_shift_on_request_count': fixed_request_count,
        'resolved_request_count': len(resolved_requests),
        'unresolved_request_count': len(unresolved_requests),
        'satisfied_request_count': len(satisfied) if state is not None else None,
        'unsatisfied_request_count': len(unsatisfied) if state is not None else None,
        'all_resolved_requests_satisfied': not unsatisfied if state is not None else None,
        'score_floor': float(score_floor),
        'score_floor_exact': str(score_floor),
        'assessment_rows': assessment_rows,
        'floor_rows': floor_rows,
        'resolved_requests': resolved_requests,
        'unresolved_requests': unresolved_requests,
    }


def _selected_physician_score(
    instances, all_physicians, selected_physician_ids, state, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
):
    """Exact additive official-score contribution for selected physicians."""
    selected_ids = set(selected_physician_ids)
    selected_physicians = [
        physician for physician in all_physicians if physician.id in selected_ids
    ]
    filtered_state = defaultdict(list, {
        instance_id: [pid for pid in physician_ids if pid in selected_ids]
        for instance_id, physician_ids in state.items()
    })
    instances_by_id = {instance.id: instance for instance in instances}
    physician_hours = defaultdict(lambda: Decimal('0'))
    physician_shifts = defaultdict(int)
    physician_night_shifts = defaultdict(int)
    request_score_total = Decimal('0')
    total_nights = 0
    total_weekends = 0

    for instance in instances:
        for _physician_id in state[instance.id]:
            if instance.shift_template.night_shift:
                total_nights += 1
            if instance.date.strftime('%A') in (instance.shift_template.weekend_days or []):
                total_weekends += 1
        for physician_id in filtered_state[instance.id]:
            physician_hours[physician_id] += _shift_hours(instance)
            physician_shifts[physician_id] += 1
            if instance.shift_template.night_shift:
                physician_night_shifts[physician_id] += 1
            contract = contract_by_physician.get(physician_id)
            if contract is not None:
                request_score, _violations, _rewards = _request_score(
                    _requests_for_shift(
                        requests_by_physician_date, physician_id, instance,
                    ),
                    contract,
                )
                request_score_total += request_score

    workload_rows = _workload_score_rows(
        selected_physicians, instances, filtered_state,
        physician_hours, physician_shifts, physician_night_shifts, targets,
    )
    workload_score = sum(
        (Decimal(row['score_contribution_exact']) for row in workload_rows),
        Decimal('0'),
    )
    underutilization_score = Decimal('0')

    for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
        if physician_id not in selected_ids:
            continue
        physician_instances = [
            instances_by_id[instance_id]
            for instance_id, physician_ids in filtered_state.items()
            if physician_id in physician_ids and instance_id in instances_by_id
        ]
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        for schedule_request in schedule_requests:
            missed = False
            if schedule_request.request_type == ScheduleRequest.RequestType.DAY_ON:
                missed = not any(
                    instance.date == request_date for instance in physician_instances
                )
            elif schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON:
                template_ids = {
                    template.id for template in schedule_request.shift_templates.all()
                }
                missed = not any(
                    instance.date == request_date
                    and instance.shift_template_id in template_ids
                    for instance in physician_instances
                )
            if missed:
                request_score_total += _request_weight(contract, schedule_request.weight)

    validation = _validate_schedule(
        instances, selected_physicians, filtered_state,
        eligible_facilities_by_physician, minimum_rest_by_physician,
    )
    validation_score = (
        Decimal(validation['final_rest_violations']) * Decimal(REST_VIOLATION_PENALTY)
        + Decimal(validation['final_overlap_violations']) * Decimal(OVERLAP_VIOLATION_PENALTY)
        + Decimal(validation['final_duplicate_violations']) * Decimal(OVERLAP_VIOLATION_PENALTY)
        + Decimal(validation['final_facility_ineligible_violations']) * Decimal(FACILITY_ELIGIBILITY_PENALTY)
        + Decimal(validation['final_inactive_physician_violations']) * Decimal(INACTIVE_PHYSICIAN_PENALTY)
    )
    physician_count = len(all_physicians)
    distribution_scores = _distribution_score(
        instances, selected_physicians, filtered_state,
        contract_by_physician, eligible_facilities_by_physician,
        default_weekend_target_override=(
            Decimal(total_weekends) / Decimal(physician_count)
            if physician_count else Decimal('0')
        ),
        default_night_target_override=(
            Decimal(total_nights) / Decimal(physician_count)
            if physician_count else Decimal('0')
        ),
    )
    return (
        workload_score + request_score_total + underutilization_score
        + validation_score + sum(distribution_scores.values(), Decimal('0'))
    )


def _selected_physician_score_delta(
    instances, physicians, state, trial_state, physician_ids, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
):
    before = _selected_physician_score(
        instances, physicians, physician_ids, state, targets,
        contract_by_physician, requests_by_physician_date,
        eligible_facilities_by_physician, minimum_rest_by_physician,
    )
    after = _selected_physician_score(
        instances, physicians, physician_ids, trial_state, targets,
        contract_by_physician, requests_by_physician_date,
        eligible_facilities_by_physician, minimum_rest_by_physician,
    )
    return after - before


def _optimizer_pairs(state, manual_pairs):
    pairs = []
    for instance_id, physician_ids in state.items():
        for physician_id in physician_ids:
            if (instance_id, physician_id) not in manual_pairs:
                pairs.append((instance_id, physician_id))
    return sorted(pairs)


def _bounded_pairwise_candidates(left_pairs, right_pairs, limit):
    """Keep traversal order without materializing the Cartesian neighborhood."""
    return islice((
        (left_instance_id, left_physician_id, right_instance_id, right_physician_id)
        for left_instance_id, left_physician_id in left_pairs
        for right_instance_id, right_physician_id in right_pairs
    ), max(0, limit))


def _project_workload_change(periods, day, hours, direction):
    """Ranking estimate only; accepted moves still require official full scoring."""
    delta = Decimal('0')
    for start, end, assigned, rule in periods:
        if not start <= day <= end:
            continue
        changed = assigned + direction * (Decimal('1') if rule['units'] == 'SHIFTS' else hours)

        def penalty(value):
            if rule['min_value'] is not None and value < rule['min_value']:
                return (rule['min_value'] - value) * rule['min_penalty_weight']
            if rule['max_value'] is not None and value > rule['max_value']:
                return (value - rule['max_value']) * rule['max_penalty_weight']
            return Decimal('0')

        delta += penalty(changed) - penalty(assigned)
    return delta


def _repair_workload_transfers(
    *, instances, physicians, state, manual_pairs, targets, contract_by_physician,
    requests_by_physician_date, eligible_facilities_by_physician,
    minimum_rest_by_physician, should_stop, candidate_limit=2000, on_improvement=None,
):
    """Revisit period-specific overload after every verified improving transfer."""
    by_id = {instance.id: instance for instance in instances}
    args = (targets, contract_by_physician, requests_by_physician_date,
            eligible_facilities_by_physician, minimum_rest_by_physician)
    scoring = _score_schedule(instances, physicians, state, *args)
    attempts = 0
    accepts = []
    reason = 'no_improving_transfer'

    def relevant(row, instance):
        return row['period_start'] <= instance.date.isoformat() <= row['period_end']

    while attempts < candidate_limit and not should_stop():
        rows = {row['physician_id']: row for row in scoring['workload_score_rows']}
        # Rows follow rule/window order, including simultaneously enforced periods.
        periods = {}
        for pid, row in rows.items():
            rules = [
                (start, end, _effective_workload_rule(rule, start, end))
                for rule in targets[pid].get('rules', [])
                for start, end in _period_windows(instances, rule['period_type'])
            ]
            periods[pid] = [
                (start, end, Decimal(str(result['assigned_value'])), rule)
                for (start, end, rule), result in zip(rules, row['rule_rows'])
            ]
        projections = {}

        def projection(pid, instance, direction):
            hours = _shift_hours(instance)
            key = (pid, instance.date, hours, direction)
            if key not in projections:
                projections[key] = _project_workload_change(
                    periods[pid], instance.date, hours, direction,
                )
            return projections[key]

        def candidates():
            for instance in instances:
                if should_stop():
                    return
                if instance.is_locked_open:
                    continue
                for donor in state[instance.id]:
                    if donor not in rows or (instance.id, donor) in manual_pairs:
                        continue
                    if not any(row['deviation_direction'] == 'above_maximum'
                               and relevant(row, instance) for row in rows[donor]['rule_rows']):
                        continue
                    removed = projection(donor, instance, -1)
                    for receiver in rows:
                        if should_stop():
                            return
                        if receiver in state[instance.id]:
                            continue
                        delta = removed + projection(receiver, instance, 1)
                        if delta < 0:
                            yield delta, instance.id, donor, receiver

        # Keep memory bounded rather than materializing the transfer neighborhood.
        ranked = nsmallest(candidate_limit - attempts, candidates())
        accepted = False
        for predicted_delta, instance_id, donor, receiver in ranked:
            instance = by_id[instance_id]
            if should_stop() or attempts >= candidate_limit:
                break
            attempts += 1
            if not _can_assign_in_state(
                state, by_id, instance, receiver,
                eligible_facilities_by_physician, minimum_rest_by_physician,
            ):
                continue
            trial = _copy_state(state)
            _replace_in_state(trial, instance.id, donor, receiver)
            delta = _selected_physician_score_delta(
                instances, physicians, state, trial, {donor, receiver}, *args,
            )
            if delta >= 0 or should_stop():
                continue
            verified = _score_schedule(instances, physicians, trial, *args)
            if (_has_hard_invalids(verified) or verified['score'] >= scoring['score']
                    or verified['breakdown']['workload_score'] >= scoring['breakdown']['workload_score']):
                continue
            accepts.append({
                'shift_instance_id': instance.id, 'from_physician_id': donor,
                'to_physician_id': receiver, 'score_before': float(scoring['score']),
                'score_after': float(verified['score']),
                'predicted_workload_delta': float(predicted_delta),
            })
            state, scoring = trial, verified
            if on_improvement is not None:
                on_improvement(state, scoring)
            accepted = True
            break
        if not accepted:
            break
    if should_stop():
        reason = 'time_budget'
    elif attempts >= candidate_limit:
        reason = 'candidate_budget'
    return state, scoring, {'attempts': attempts, 'accepts': accepts, 'stopped_reason': reason}


def _run_productive_repair_rounds(repair, *, state, elapsed, deadline,
                                  candidate_limit, round_seconds=4, max_rounds=5, stop_requested=None, **kwargs):
    """Renew a short time slice only after progress; never exceed the shared deadline."""
    started = elapsed()
    debug = {'attempts': 0, 'accepts': [], 'rounds': [], 'stopped_reason': 'time_budget'}
    scoring = None
    for _ in range(max_rounds):
        now = elapsed()
        remaining = candidate_limit - debug['attempts']
        if now >= deadline or remaining <= 0 or (stop_requested is not None and stop_requested()):
            break
        round_deadline = min(deadline, now + round_seconds)
        state, scoring, result = repair(
            state=state, candidate_limit=remaining,
            should_stop=lambda: elapsed() >= round_deadline or (stop_requested is not None and stop_requested()), **kwargs,
        )
        debug['attempts'] += result['attempts']
        debug['accepts'].extend(result['accepts'])
        debug['rounds'].append({
            'runtime_seconds': elapsed() - now, 'attempts': result['attempts'],
            'improvements': len(result['accepts']), 'stopped_reason': result['stopped_reason'],
        })
        debug['stopped_reason'] = result['stopped_reason']
        if not result['accepts'] or result['stopped_reason'] != 'time_budget':
            break
    if debug['attempts'] >= candidate_limit:
        debug['stopped_reason'] = 'candidate_budget'
    elif elapsed() >= deadline:
        debug['stopped_reason'] = 'time_budget'
    elif len(debug['rounds']) == max_rounds and debug['stopped_reason'] == 'time_budget':
        debug['stopped_reason'] = 'round_budget'
    debug['runtime_seconds'] = elapsed() - started
    return state, scoring, debug


def _repair_night_minimum_distribution(
    *, instances, physicians, state, manual_pairs, targets, contract_by_physician,
    requests_by_physician_date, eligible_facilities_by_physician,
    minimum_rest_by_physician, should_stop, candidate_limit=500, on_improvement=None,
):
    """Move nights toward contract minimum deficits using verified legal improvements."""
    by_id = {instance.id: instance for instance in instances}
    locked_open = {instance.id for instance in instances if instance.is_locked_open}
    args = (targets, contract_by_physician, requests_by_physician_date,
            eligible_facilities_by_physician, minimum_rest_by_physician)
    scoring = _score_schedule(instances, physicians, state, *args)
    attempts, accepts = 0, []

    def covers(row, instance):
        day = instance.date.isoformat()
        return row['period_start'] <= day <= row['period_end']

    while attempts < candidate_limit and not should_stop():
        status = _night_minimum_status(
            instances, physicians, state, contract_by_physician,
        )
        under_rows = status['physicians_under_night_minimum']
        over_rows = status['physicians_over_night_minimum']
        if not under_rows or not over_rows:
            break

        night_pairs = [
            (instance_id, physician_id)
            for instance_id, physician_id in _optimizer_pairs(state, manual_pairs)
            if instance_id not in locked_open
            and by_id[instance_id].shift_template.night_shift
        ]
        non_night_pairs = [
            (instance_id, physician_id)
            for instance_id, physician_id in _optimizer_pairs(state, manual_pairs)
            if instance_id not in locked_open
            and not by_id[instance_id].shift_template.night_shift
        ]
        candidates = []
        for night_instance_id, donor in night_pairs:
            night_instance = by_id[night_instance_id]
            donor_surplus = max(
                (row['actual'] - row['minimum'] for row in over_rows
                 if row['physician_id'] == donor and covers(row, night_instance)),
                default=0,
            )
            if donor_surplus <= 0:
                continue
            for under_row in under_rows:
                receiver = under_row['physician_id']
                if receiver == donor or receiver in state[night_instance_id]:
                    continue
                if not covers(under_row, night_instance):
                    continue
                deficit = under_row['minimum'] - under_row['actual']
                priority = (-deficit * under_row['penalty_weight'], -donor_surplus,
                            night_instance_id, donor, receiver)
                candidates.append((priority, night_instance_id, donor, receiver, None))
                for non_night_instance_id, owner in non_night_pairs:
                    if owner == receiver:
                        candidates.append((priority, night_instance_id, donor, receiver,
                                           non_night_instance_id))
        candidates.sort(key=lambda item: item[0])

        accepted = False
        for _, night_instance_id, donor, receiver, non_night_instance_id in candidates:
            if attempts >= candidate_limit or should_stop():
                break
            attempts += 1
            night_instance = by_id[night_instance_id]
            trial = _copy_state(state)
            _replace_in_state(trial, night_instance_id, donor, receiver)
            if not _can_assign_in_state(
                trial, by_id, night_instance, receiver,
                eligible_facilities_by_physician, minimum_rest_by_physician,
                exclude_instance_id=night_instance_id,
            ):
                continue
            move_kind = 'reassignment'
            if non_night_instance_id is not None:
                non_night_instance = by_id[non_night_instance_id]
                _replace_in_state(trial, non_night_instance_id, receiver, donor)
                if not _can_assign_in_state(
                    trial, by_id, non_night_instance, donor,
                    eligible_facilities_by_physician, minimum_rest_by_physician,
                    exclude_instance_id=non_night_instance_id,
                ):
                    continue
                move_kind = 'exchange'
            verified = _score_schedule(instances, physicians, trial, *args)
            if _has_hard_invalids(verified) or verified['score'] >= scoring['score']:
                continue
            accepts.append({
                'kind': move_kind,
                'night_shift_instance_id': night_instance_id,
                'non_night_shift_instance_id': non_night_instance_id,
                'from_physician_id': donor,
                'to_physician_id': receiver,
                'score_before': float(scoring['score']),
                'score_after': float(verified['score']),
            })
            state, scoring = trial, verified
            if on_improvement is not None:
                on_improvement(state, scoring)
            accepted = True
            break
        if not accepted:
            break

    reason = 'no_improving_move'
    if should_stop():
        reason = 'time_budget'
    elif attempts >= candidate_limit:
        reason = 'candidate_budget'
    return state, scoring, {
        'attempts': attempts, 'accepts': accepts, 'stopped_reason': reason,
    }


def _repair_night_spacing_swaps(
    *, instances, physicians, state, manual_pairs, targets, contract_by_physician,
    requests_by_physician_date, eligible_facilities_by_physician,
    minimum_rest_by_physician, should_stop, candidate_limit=500, maximum_only=False, on_improvement=None,
):
    """Bounded recovery or maximum repairs; preserve workload and official validity."""
    by_id = {instance.id: instance for instance in instances}
    locked_open = {instance.id for instance in instances if instance.is_locked_open}
    groups = defaultdict(list)
    for instance in instances:
        groups[(instance.date.year, instance.date.month, _shift_hours(instance))].append(instance)
    args = (targets, contract_by_physician, requests_by_physician_date,
            eligible_facilities_by_physician, minimum_rest_by_physician)
    scoring = _score_schedule(instances, physicians, state, *args)
    attempts, accepts = 0, []
    while attempts < candidate_limit and not should_stop():
        report = _night_violation_report(instances, physicians, state, contract_by_physician)
        maximum_excess = sum(max(0, row['actual_value'] - row['configured_limit'])
                             for row in report['night_violations']
                             if row['violation_type'] == 'NIGHT_OVER_MAXIMUM') if maximum_only else 0
        night_counts = Counter(pid for instance in instances if instance.shift_template.night_shift
                               for pid in state[instance.id]) if maximum_only else {}
        sources = {}
        for row in report['night_violations']:
            kinds = ('NIGHT_OVER_MAXIMUM',) if maximum_only else (
                'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT',
                'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK',
            )
            if row['violation_type'] not in kinds:
                continue
            for sid in row.get('shift_instance_ids', []):
                pair = (sid, row['physician_id'])
                if sid in by_id and pair not in manual_pairs and sid not in locked_open:
                    sources[pair] = sources.get(pair, Decimal('0')) + Decimal(str(row['penalty']))
        accepted = False
        visited = set()
        for (sid, donor), _ in sorted(sources.items(), key=lambda item: (-item[1], item[0])):
            left = by_id[sid]
            if donor not in state[sid]:
                continue
            if maximum_only and not left.shift_template.night_shift:
                continue
            candidates = groups[(left.date.year, left.date.month, _shift_hours(left))]
            if maximum_only:
                candidates = sorted(
                    (right for right in candidates if not right.shift_template.night_shift),
                    key=lambda right: (min((night_counts[pid] for pid in state[right.id]), default=0),
                                       abs((right.date - left.date).days), right.id),
                )
            for right in candidates:
                if right.id == sid or right.id in locked_open:
                    continue
                for receiver in state[right.id]:
                    if should_stop() or attempts >= candidate_limit:
                        break
                    if receiver == donor or (right.id, receiver) in manual_pairs:
                        continue
                    key = tuple(sorted(((sid, donor), (right.id, receiver))))
                    if key in visited:
                        continue
                    visited.add(key)
                    attempts += 1
                    result = evaluate_plateau_pairwise_swap(
                        instances=instances, physicians=physicians, state=state,
                        instances_by_id=by_id, manual_pairs=manual_pairs,
                        locked_open_instance_ids=locked_open, targets=targets,
                        contract_by_physician=contract_by_physician,
                        requests_by_physician_date=requests_by_physician_date,
                        eligible_facilities_by_physician=eligible_facilities_by_physician,
                        minimum_rest_by_physician=minimum_rest_by_physician,
                        current_score=scoring['score'], left_instance_id=sid,
                        left_physician_id=donor, right_instance_id=right.id,
                        right_physician_id=receiver,
                    )
                    verified = result.get('scoring')
                    if (not result.get('legal') or not verified or _has_hard_invalids(verified)
                            or verified['score'] >= scoring['score']
                            or verified['breakdown']['night_score'] >= scoring['breakdown']['night_score']
                            or verified['breakdown']['workload_score'] > scoring['breakdown']['workload_score']):
                        continue
                    if maximum_only:
                        trial_report = _night_violation_report(
                            instances, physicians, result['state'], contract_by_physician,
                        )
                        trial_excess = sum(max(0, row['actual_value'] - row['configured_limit'])
                                           for row in trial_report['night_violations']
                                           if row['violation_type'] == 'NIGHT_OVER_MAXIMUM')
                        if trial_excess >= maximum_excess:
                            continue
                    accepts.append({
                        'left_instance_id': sid, 'right_instance_id': right.id,
                        'left_physician_id': donor, 'right_physician_id': receiver,
                        'score_before': float(scoring['score']), 'score_after': float(verified['score']),
                        'night_delta': float(verified['breakdown']['night_score'] - scoring['breakdown']['night_score']),
                    })
                    state, scoring = result['state'], verified
                    if on_improvement is not None:
                        on_improvement(state, scoring)
                    accepted = True
                    break
                if accepted or should_stop() or attempts >= candidate_limit:
                    break
            if accepted or should_stop() or attempts >= candidate_limit:
                break
        if not accepted:
            break
    reason = 'time_budget' if should_stop() else ('candidate_budget' if attempts >= candidate_limit else 'no_improving_swap')
    return state, scoring, {'attempts': attempts, 'accepts': accepts, 'stopped_reason': reason}


def _round_robin_candidates(streams):
    pending = deque(iter(stream) for stream in streams)
    while pending:
        stream = pending.popleft()
        try:
            candidate = next(stream)
        except StopIteration:
            continue
        yield candidate
        pending.append(stream)


def _repair_recovery_day_swaps(
    *, instances, physicians, state, manual_pairs, targets, contract_by_physician,
    requests_by_physician_date, eligible_facilities_by_physician,
    minimum_rest_by_physician, should_stop, candidate_limit=600, on_improvement=None,
):
    """Try nearby equal-hour day swaps fairly across recovery-conflict physicians."""
    by_id = {x.id: x for x in instances}
    locked_open = {x.id for x in instances if x.is_locked_open}
    groups = defaultdict(list)
    for x in instances:
        if not x.shift_template.night_shift:
            groups[(x.date.year, x.date.month, _shift_hours(x))].append(x)
    args = (targets, contract_by_physician, requests_by_physician_date,
            eligible_facilities_by_physician, minimum_rest_by_physician)
    scoring = _score_schedule(instances, physicians, state, *args)
    attempts, accepts = 0, []
    last_donor = None
    while attempts < candidate_limit and not should_stop():
        sources = defaultdict(set)
        for row in _night_violation_report(instances, physicians, state, contract_by_physician)['night_violations']:
            if row['violation_type'] != 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT':
                continue
            sid = (row.get('next_assignment') or {}).get('shift_instance_id')
            pid = row['physician_id']
            if (sid in by_id and not by_id[sid].shift_template.night_shift
                    and sid not in locked_open and (sid, pid) not in manual_pairs
                    and pid in state[sid]):
                sources[pid].add(sid)

        def source_candidates(pid, sid):
            left = by_id[sid]
            candidates = groups[(left.date.year, left.date.month, _shift_hours(left))]
            for right in sorted(candidates, key=lambda x: (abs((x.date - left.date).days), x.id)):
                if should_stop():
                    return
                if right.date == left.date or right.id in locked_open:
                    continue
                for receiver in state[right.id]:
                    if receiver != pid and (right.id, receiver) not in manual_pairs:
                        yield sid, pid, right.id, receiver

        # One candidate per physician per cycle; also rotate their own conflicts.
        pids = sorted(sources)
        if last_donor in pids:
            offset = pids.index(last_donor) + 1
            pids = pids[offset:] + pids[:offset]
        def physician_candidates(pid):
            return _round_robin_candidates(source_candidates(pid, sid) for sid in sorted(sources[pid]))

        streams = [physician_candidates(pid) for pid in pids]
        visited = set()
        accepted = False
        for sid, donor, rid, receiver in _round_robin_candidates(streams):
            if should_stop() or attempts >= candidate_limit:
                break
            key = tuple(sorted(((sid, donor), (rid, receiver))))
            if key in visited:
                continue
            visited.add(key)
            attempts += 1
            result = evaluate_plateau_pairwise_swap(
                instances=instances, physicians=physicians, state=state,
                instances_by_id=by_id, manual_pairs=manual_pairs,
                locked_open_instance_ids=locked_open, targets=targets,
                contract_by_physician=contract_by_physician,
                requests_by_physician_date=requests_by_physician_date,
                eligible_facilities_by_physician=eligible_facilities_by_physician,
                minimum_rest_by_physician=minimum_rest_by_physician,
                current_score=scoring['score'], left_instance_id=sid,
                left_physician_id=donor, right_instance_id=rid, right_physician_id=receiver,
            )
            verified = result.get('scoring')
            if (not result.get('legal') or not verified or _has_hard_invalids(verified)
                    or verified['score'] >= scoring['score']
                    or verified['breakdown']['night_score'] >= scoring['breakdown']['night_score']
                    or verified['breakdown']['workload_score'] > scoring['breakdown']['workload_score']):
                continue
            accepts.append({'left_instance_id': sid, 'right_instance_id': rid,
                            'left_physician_id': donor, 'right_physician_id': receiver,
                            'score_before': float(scoring['score']), 'score_after': float(verified['score'])})
            state, scoring = result['state'], verified
            if on_improvement is not None:
                on_improvement(state, scoring)
            last_donor = donor
            accepted = True
            break
        if not accepted:
            break
    reason = 'time_budget' if should_stop() else ('candidate_budget' if attempts >= candidate_limit else 'no_improving_swap')
    return state, scoring, {'attempts': attempts, 'accepts': accepts, 'stopped_reason': reason}


def _copy_state(state):
    copied = defaultdict(list)
    for instance_id, physician_ids in state.items():
        copied[instance_id] = list(physician_ids)
    return copied


def _physician_workload_rows(physician_ids, scoring, targets):
    rows = []
    for physician_id in physician_ids:
        target = targets[physician_id]
        workload_value = _workload_value(
            target,
            scoring['physician_hours'],
            scoring['physician_shifts'],
            physician_id,
        )
        rows.append((workload_value - (target['target'] or Decimal('0')), physician_id))
    return rows


def _middle_out(items):
    if not items:
        return []
    middle = len(items) // 2
    ordered = [items[middle]]
    for offset in range(1, len(items)):
        left = middle - offset
        right = middle + offset
        if left >= 0:
            ordered.append(items[left])
        if right < len(items):
            ordered.append(items[right])
    return ordered


def _same_shift_break_candidates(instances, physicians, state, manual_pairs, contract_by_physician):
    candidates = []
    _score, violations = _same_shift_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
    )
    for violation in violations:
        physician_id = violation['physician_id']
        movable_instance_ids = [
            instance_id
            for instance_id in violation['shift_instance_ids']
            if (instance_id, physician_id) not in manual_pairs
        ]
        for instance_id in _middle_out(movable_instance_ids):
            candidates.append((physician_id, instance_id))
    return candidates


def _request_repair_candidates(instances, physicians, state, manual_pairs, contract_by_physician, requests_by_physician_date):
    rows = _request_scoring_rows(
        instances,
        physicians,
        state,
        contract_by_physician,
        requests_by_physician_date,
    )
    candidates = []
    for row in rows:
        if row['violation_type'] not in {'REQUEST_DAY_OFF_VIOLATION', 'REQUEST_SHIFT_OFF_VIOLATION'}:
            continue
        for instance_id in row.get('shift_instance_ids') or []:
            physician_id = row['physician_id']
            if (instance_id, physician_id) in manual_pairs:
                continue
            candidates.append(
                (
                    -Decimal(str(row.get('penalty', 0))),
                    physician_id,
                    instance_id,
                )
            )

    # Unmet request-on rows do not refer to an existing assignment, so they
    # cannot be handled by the violation-row loop above. Add the current owner
    # of each matching shift as a reassignment source. This is especially
    # important for FIXED requests: increasing their penalty is otherwise
    # ineffective because the optimizer never proposes the requested move.
    instances_by_date = defaultdict(list)
    for instance in instances:
        instances_by_date[instance.date].append(instance)
    for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
        contract = contract_by_physician.get(physician_id)
        if contract is None or getattr(contract, 'manual_assignment_only', False):
            continue
        for schedule_request in schedule_requests:
            if schedule_request.request_type not in {
                ScheduleRequest.RequestType.DAY_ON,
                ScheduleRequest.RequestType.SHIFT_ON,
            }:
                continue
            matching_instances = instances_by_date.get(request_date, [])
            if schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON:
                template_ids = {
                    template.id for template in schedule_request.shift_templates.all()
                }
                matching_instances = [
                    instance for instance in matching_instances
                    if instance.shift_template_id in template_ids
                ]
            if any(physician_id in state[instance.id] for instance in matching_instances):
                continue
            penalty = _request_weight(contract, schedule_request.weight)
            if (
                penalty <= 0
                and schedule_request.weight != ScheduleRequest.Weight.FIXED
            ):
                continue
            for instance in matching_instances:
                for current_owner_id in state[instance.id]:
                    if (instance.id, current_owner_id) in manual_pairs:
                        continue
                    candidates.append((-penalty, current_owner_id, instance.id))

    result = []
    seen = set()
    for _penalty, physician_id, instance_id in sorted(candidates):
        key = (physician_id, instance_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _request_on_repair_candidates(instances, state, manual_pairs, contract_by_physician, requests_by_physician_date):
    """Return unmet request-on moves while retaining the intended recipient."""
    instances_by_date = defaultdict(list)
    for instance in instances:
        instances_by_date[instance.date].append(instance)

    candidates = []
    for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
        contract = contract_by_physician.get(physician_id)
        if contract is None or getattr(contract, 'manual_assignment_only', False):
            continue
        for schedule_request in schedule_requests:
            if schedule_request.request_type not in {
                ScheduleRequest.RequestType.DAY_ON,
                ScheduleRequest.RequestType.SHIFT_ON,
            }:
                continue
            matching_instances = instances_by_date.get(request_date, [])
            if schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON:
                template_ids = {
                    template.id for template in schedule_request.shift_templates.all()
                }
                matching_instances = [
                    instance for instance in matching_instances
                    if instance.shift_template_id in template_ids
                ]
            if any(physician_id in state[instance.id] for instance in matching_instances):
                continue
            penalty = _request_weight(contract, schedule_request.weight)
            if (
                penalty <= 0
                and schedule_request.weight != ScheduleRequest.Weight.FIXED
            ):
                continue
            for instance in matching_instances:
                for current_owner_id in state[instance.id]:
                    if (instance.id, current_owner_id) in manual_pairs:
                        continue
                    candidates.append(
                        (-penalty, physician_id, current_owner_id, instance.id)
                    )

    result = []
    seen = set()
    for _penalty, physician_id, current_owner_id, instance_id in sorted(candidates):
        key = (physician_id, current_owner_id, instance_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _fixed_request_on_unmet_count(instances, state, requests_by_physician_date):
    """Count unmet FIXED day/shift-on requests for lexicographic optimization."""
    instances_by_id = {instance.id: instance for instance in instances}
    assigned_by_physician = defaultdict(list)
    for instance_id, physician_ids in state.items():
        instance = instances_by_id.get(instance_id)
        if instance is None:
            continue
        for physician_id in physician_ids:
            assigned_by_physician[physician_id].append(instance)

    unmet = 0
    for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
        physician_instances = assigned_by_physician.get(physician_id, ())
        for schedule_request in schedule_requests:
            if schedule_request.weight != ScheduleRequest.Weight.FIXED:
                continue
            if schedule_request.request_type == ScheduleRequest.RequestType.DAY_ON:
                satisfied = any(instance.date == request_date for instance in physician_instances)
            elif schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON:
                template_ids = {template.id for template in schedule_request.shift_templates.all()}
                satisfied = any(
                    instance.date == request_date and instance.shift_template_id in template_ids
                    for instance in physician_instances
                )
            else:
                continue
            if not satisfied:
                unmet += 1
    return unmet


def _workload_repair_candidates(instances, state, manual_pairs, scoring):
    rows = scoring.get('workload_score_rows', [])
    by_physician = {row['physician_id']: row for row in rows}
    over_rows = [
        row for row in rows
        if row.get('deviation_direction') == 'above_maximum'
    ]
    receiver_rows = [
        row for row in rows
        if row.get('deviation_direction') in {'below_minimum', 'inside_range'}
    ]
    over_ids = {row['physician_id'] for row in over_rows}
    receiver_ids = {row['physician_id'] for row in receiver_rows}
    receiver_priority = {
        row['physician_id']: (
            0 if row.get('deviation_direction') == 'below_minimum' else 1,
            -Decimal(str(row.get('score_contribution_exact', row.get('score_contribution', 0)))),
            Decimal(str(row.get('assigned_hours', 0))),
            row['physician_id'],
        )
        for row in receiver_rows
    }
    candidates = []
    for instance in sorted(instances, key=lambda item: (-_shift_hours(item), item.date, item.id)):
        for physician_id in state[instance.id]:
            if physician_id not in over_ids:
                continue
            if (instance.id, physician_id) in manual_pairs:
                continue
            for receiver_id in sorted(
                receiver_ids,
                key=lambda item: (
                    -Decimal(str(by_physician.get(physician_id, {}).get('score_contribution_exact', 0))),
                    receiver_priority.get(item, (2, Decimal('0'), Decimal('0'), item)),
                ),
            ):
                if receiver_id == physician_id:
                    continue
                candidates.append((physician_id, receiver_id, instance.id))
    return candidates


def _workload_range_counts(scoring):
    rows = scoring.get('workload_score_rows', [])
    return {
        'over': sum(1 for row in rows if row.get('deviation_direction') == 'above_maximum'),
        'under': sum(1 for row in rows if row.get('deviation_direction') == 'below_minimum'),
    }


def _workload_repair_swap_candidates(instances, state, manual_pairs, scoring):
    rows = scoring.get('workload_score_rows', [])
    over_rows = sorted(
        [
            row for row in rows
            if row.get('deviation_direction') == 'above_maximum'
        ],
        key=lambda row: (
            -Decimal(str(row.get('score_contribution_exact', row.get('score_contribution', 0)))),
            -Decimal(str(row.get('assigned_hours', 0))),
            row['physician_id'],
        ),
    )
    receiver_rows = sorted(
        [
            row for row in rows
            if row.get('deviation_direction') in {'below_minimum', 'inside_range'}
        ],
        key=lambda row: (
            0 if row.get('deviation_direction') == 'below_minimum' else 1,
            -Decimal(str(row.get('score_contribution_exact', row.get('score_contribution', 0)))),
            Decimal(str(row.get('assigned_hours', 0))),
            row['physician_id'],
        ),
    )
    over_ids = {row['physician_id'] for row in over_rows}
    receiver_ids = {row['physician_id'] for row in receiver_rows}
    assignments_by_physician = defaultdict(list)
    for instance_id, physician_id in _optimizer_pairs(state, manual_pairs):
        if physician_id not in over_ids and physician_id not in receiver_ids:
            continue
        instance = next((item for item in instances if item.id == instance_id), None)
        if instance is None:
            continue
        assignments_by_physician[physician_id].append(instance)

    candidates = []
    for over_row in over_rows:
        over_id = over_row['physician_id']
        over_instances = sorted(
            assignments_by_physician.get(over_id, []),
            key=lambda item: (-_shift_hours(item), item.date, item.id),
        )
        for receiver_row in receiver_rows:
            receiver_id = receiver_row['physician_id']
            if receiver_id == over_id:
                continue
            receiver_instances = sorted(
                assignments_by_physician.get(receiver_id, []),
                key=lambda item: (_shift_hours(item), item.date, item.id),
            )
            for over_instance in over_instances:
                over_hours = _shift_hours(over_instance)
                for receiver_instance in receiver_instances:
                    if over_instance.id == receiver_instance.id:
                        continue
                    if over_hours <= _shift_hours(receiver_instance):
                        continue
                    candidates.append((over_id, receiver_id, over_instance.id, receiver_instance.id))
    return candidates


def _consecutive_day_break_candidates(state, instances_by_id, manual_pairs, contract_by_physician):
    candidates = []
    dates_by_physician = defaultdict(lambda: defaultdict(list))
    for instance_id, physician_ids in state.items():
        instance = instances_by_id.get(instance_id)
        if instance is None:
            continue
        for physician_id in physician_ids:
            dates_by_physician[physician_id][instance.date].append(instance)

    for physician_id, instances_by_date in dates_by_physician.items():
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        settings = contract.workload_settings if isinstance(contract.workload_settings, dict) else {}
        max_streak_value = _decimal_or_none(settings.get('max_days_in_row'))
        penalty_value = _decimal_or_none(settings.get('max_days_in_row_penalty_weight'))
        if (
            max_streak_value is None or max_streak_value <= 0
            or penalty_value is None or penalty_value <= 0
        ):
            continue
        max_streak = max(int(max_streak_value), 1)
        streak_dates = []
        previous_date = None
        for current_date in sorted(instances_by_date):
            if previous_date is None or current_date == previous_date + timedelta(days=1):
                streak_dates.append(current_date)
            else:
                if len(streak_dates) > max_streak:
                    for streak_date in _middle_out(streak_dates):
                        movable = [
                            item for item in instances_by_date[streak_date]
                            if (item.id, physician_id) not in manual_pairs
                        ]
                        candidates.extend((physician_id, item.id) for item in _middle_out(movable))
                streak_dates = [current_date]
            previous_date = current_date
        if len(streak_dates) > max_streak:
            for streak_date in _middle_out(streak_dates):
                movable = [
                    item for item in instances_by_date[streak_date]
                    if (item.id, physician_id) not in manual_pairs
                ]
                candidates.extend((physician_id, item.id) for item in _middle_out(movable))
    return candidates


def _repair_general_constraint_reassignments(
    *, instances, physicians, state, manual_pairs, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
    should_stop=lambda: False, candidate_limit=600, on_improvement=None,
):
    """Revisit non-night constraints during extended search.

    Sources are constraint-specific, but acceptance uses the complete score so
    improving one rule cannot make the overall schedule worse.
    """
    current = _copy_state(state)
    instances_by_id = {instance.id: instance for instance in instances}
    current_scoring = _score_schedule(
        instances, physicians, current, targets, contract_by_physician,
        requests_by_physician_date, eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    source_groups = (
        ('request', _request_repair_candidates(
            instances, physicians, current, manual_pairs, contract_by_physician,
            requests_by_physician_date)),
        ('weekend', _weekend_repair_candidates(
            instances, physicians, current, manual_pairs,
            contract_by_physician)),
        ('same_shift', _same_shift_break_candidates(
            instances, physicians, current, manual_pairs, contract_by_physician)),
        ('consecutive_days', _consecutive_day_break_candidates(
            current, instances_by_id, manual_pairs, contract_by_physician)),
    )
    sources, seen = [], set()
    for source_kind, candidates in source_groups:
        for physician_id, instance_id in candidates:
            key = (physician_id, instance_id)
            if key not in seen:
                seen.add(key)
                sources.append((source_kind, physician_id, instance_id))

    debug = {'attempts': 0, 'legal_candidates': 0, 'scored_candidates': 0,
             'accepts': [], 'stopped_reason': 'candidates_exhausted',
             'source_counts': {name: len(rows) for name, rows in source_groups}}
    physician_ids = sorted(physician.id for physician in physicians)
    for source_kind, from_physician_id, instance_id in sources:
        if should_stop() or debug['attempts'] >= candidate_limit:
            debug['stopped_reason'] = 'runtime_or_candidate_limit'
            break
        instance = instances_by_id.get(instance_id)
        if instance is None or from_physician_id not in current[instance_id]:
            continue
        if (instance_id, from_physician_id) in manual_pairs:
            continue
        for to_physician_id in physician_ids:
            if should_stop() or debug['attempts'] >= candidate_limit:
                debug['stopped_reason'] = 'runtime_or_candidate_limit'
                break
            if to_physician_id == from_physician_id or to_physician_id in current[instance_id]:
                continue
            debug['attempts'] += 1
            trial = _copy_state(current)
            _replace_in_state(trial, instance_id, from_physician_id, to_physician_id)
            if not _can_assign_in_state(
                trial, instances_by_id, instance, to_physician_id,
                eligible_facilities_by_physician, minimum_rest_by_physician,
                exclude_instance_id=instance_id,
            ):
                continue
            debug['legal_candidates'] += 1
            trial_scoring = _score_schedule(
                instances, physicians, trial, targets, contract_by_physician,
                requests_by_physician_date, eligible_facilities_by_physician,
                minimum_rest_by_physician,
            )
            debug['scored_candidates'] += 1
            if trial_scoring['score'] >= current_scoring['score']:
                continue
            debug['accepts'].append({
                'source': source_kind, 'shift_instance_id': instance_id,
                'from_physician_id': from_physician_id,
                'to_physician_id': to_physician_id,
                'score_before': float(current_scoring['score']),
                'score_after': float(trial_scoring['score']),
            })
            current, current_scoring = trial, trial_scoring
            if on_improvement:
                on_improvement(current, current_scoring)
            break
    return current, current_scoring, debug


def _repair_general_constraint_swaps(
    *, instances, physicians, state, manual_pairs, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
    should_stop=lambda: False, candidate_limit=600, on_improvement=None,
):
    """Exchange two movable assignments to escape single-move local minima.

    Constraint reports identify promising source assignments; legality and the
    complete objective decide whether an exchange is accepted.
    """
    current = _copy_state(state)
    instances_by_id = {instance.id: instance for instance in instances}
    current_scoring = _score_schedule(
        instances, physicians, current, targets, contract_by_physician,
        requests_by_physician_date, eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    source_groups = (
        ('request', _request_repair_candidates(
            instances, physicians, current, manual_pairs, contract_by_physician,
            requests_by_physician_date)),
        ('weekend', _weekend_repair_candidates(
            instances, physicians, current, manual_pairs,
            contract_by_physician)),
        ('same_shift', _same_shift_break_candidates(
            instances, physicians, current, manual_pairs, contract_by_physician)),
        ('consecutive_days', _consecutive_day_break_candidates(
            current, instances_by_id, manual_pairs, contract_by_physician)),
    )
    sources, seen = [], set()
    for source_kind, candidates in source_groups:
        for physician_id, instance_id in candidates:
            key = (physician_id, instance_id)
            if key not in seen:
                seen.add(key)
                sources.append((source_kind, physician_id, instance_id))

    assignments_by_physician = defaultdict(list)
    for instance_id, physician_ids in current.items():
        instance = instances_by_id.get(instance_id)
        if instance is None:
            continue
        for physician_id in physician_ids:
            if (instance_id, physician_id) not in manual_pairs:
                assignments_by_physician[physician_id].append(instance)

    debug = {'attempts': 0, 'accepts': [], 'stopped_reason': 'candidates_exhausted',
             'source_counts': {name: len(rows) for name, rows in source_groups}}
    physician_ids = sorted(physician.id for physician in physicians)
    for source_kind, left_physician_id, left_instance_id in sources:
        if should_stop() or debug['attempts'] >= candidate_limit:
            debug['stopped_reason'] = 'runtime_or_candidate_limit'
            break
        left_instance = instances_by_id.get(left_instance_id)
        if left_instance is None or left_physician_id not in current[left_instance_id]:
            continue
        if (left_instance_id, left_physician_id) in manual_pairs:
            continue
        for right_physician_id in physician_ids:
            if should_stop() or debug['attempts'] >= candidate_limit:
                debug['stopped_reason'] = 'runtime_or_candidate_limit'
                break
            if right_physician_id == left_physician_id or right_physician_id in current[left_instance_id]:
                continue
            right_instances = sorted(
                assignments_by_physician.get(right_physician_id, ()),
                key=lambda item: (
                    abs(_shift_hours(item) - _shift_hours(left_instance)),
                    abs((item.date - left_instance.date).days),
                    item.id,
                ),
            )
            for right_instance in right_instances:
                if should_stop() or debug['attempts'] >= candidate_limit:
                    debug['stopped_reason'] = 'runtime_or_candidate_limit'
                    break
                if right_instance.id == left_instance_id:
                    continue
                if left_physician_id in current[right_instance.id]:
                    continue
                if right_physician_id not in current[right_instance.id]:
                    continue
                if (right_instance.id, right_physician_id) in manual_pairs:
                    continue
                debug['attempts'] += 1
                trial = _copy_state(current)
                _replace_in_state(trial, left_instance_id, left_physician_id, right_physician_id)
                _replace_in_state(trial, right_instance.id, right_physician_id, left_physician_id)
                if not _can_assign_in_state(
                    trial, instances_by_id, left_instance, right_physician_id,
                    eligible_facilities_by_physician, minimum_rest_by_physician,
                    exclude_instance_id=left_instance_id,
                ):
                    continue
                if not _can_assign_in_state(
                    trial, instances_by_id, right_instance, left_physician_id,
                    eligible_facilities_by_physician, minimum_rest_by_physician,
                    exclude_instance_id=right_instance.id,
                ):
                    continue
                trial_scoring = _score_schedule(
                    instances, physicians, trial, targets, contract_by_physician,
                    requests_by_physician_date, eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                )
                if trial_scoring['score'] >= current_scoring['score']:
                    continue
                debug['accepts'].append({
                    'source': source_kind,
                    'left_shift_instance_id': left_instance_id,
                    'left_physician_id': left_physician_id,
                    'right_shift_instance_id': right_instance.id,
                    'right_physician_id': right_physician_id,
                    'score_before': float(current_scoring['score']),
                    'score_after': float(trial_scoring['score']),
                })
                current, current_scoring = trial, trial_scoring
                assignments_by_physician[left_physician_id] = [
                    item for item in assignments_by_physician[left_physician_id]
                    if item.id != left_instance_id
                ] + [right_instance]
                assignments_by_physician[right_physician_id] = [
                    item for item in assignments_by_physician[right_physician_id]
                    if item.id != right_instance.id
                ] + [left_instance]
                if on_improvement:
                    on_improvement(current, current_scoring)
                break
            else:
                continue
            if debug['accepts'] and debug['accepts'][-1]['left_shift_instance_id'] == left_instance_id:
                break
    return current, current_scoring, debug


def _solve_bounded_multi_physician_neighborhood(
    *, instances, physicians, state, scoring, manual_pairs, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
    rng, time_limit_seconds=4, focus_physician_ids=None,
    focus_instance_ids=None,
    focus_start=None, focus_end=None, cohort_size=10,
    allow_non_improving=False, maximum_score_increase=None,
    diversification_bias=False, should_stop=None, clock=monotonic,
):
    """Rebuild a coupled cohort atomically with CP-SAT.

    Ordinary repairs cross only one edge of the assignment graph at a time.
    This neighborhood can move hundreds of related assignments together, then
    accepts the result only after the authoritative full scorer validates it.
    """
    debug = {'attempts': 0, 'candidates': 0, 'accepted': False}
    should_stop = should_stop or (lambda: False)
    neighborhood_deadline = clock() + max(float(time_limit_seconds), 0)

    def deadline_reached(stage):
        if should_stop() or clock() >= neighborhood_deadline:
            debug['stopped'] = True
            debug['stopped_stage'] = stage
            return True
        return False

    if deadline_reached('before_model_build'):
        return _copy_state(state), scoring, debug
    penalty_by_physician = defaultdict(lambda: Decimal('0'))
    for row in scoring.get('workload_score_rows', []):
        penalty_by_physician[row['physician_id']] += Decimal(
            row.get('score_contribution_exact', '0')
        )
    night_report = _night_violation_report(
        instances, physicians, state, contract_by_physician,
    )
    for violation in night_report.get('night_violations', []):
        penalty_by_physician[violation['physician_id']] += Decimal(str(
            violation.get('penalty_amount', violation.get('penalty', 0))
        ))
    for violation in scoring.get('same_shift_violations', []):
        penalty_by_physician[violation['physician_id']] += Decimal(str(
            violation.get('penalty_amount', violation.get('penalty', 0))
        ))
    for violation in _weekend_volume_report(
        instances, physicians, state, contract_by_physician, details=True,
    ).get('violations', []):
        penalty_by_physician[violation['physician_id']] += Decimal(str(
            violation.get('penalty_amount', violation.get('penalty', 0))
        ))
    for violation in _shift_period_report(
        instances, physicians, state, contract_by_physician, details=True,
    ).get('violations', []):
        penalty_by_physician[violation['physician_id']] += Decimal(str(
            violation.get('penalty_amount', violation.get('penalty', 0))
        ))
    for violation in _request_scoring_rows(
        instances, physicians, state, contract_by_physician,
        requests_by_physician_date,
    ):
        penalty_by_physician[violation['physician_id']] += Decimal(str(
            violation.get('penalty_amount', violation.get('penalty', 0))
        ))

    if deadline_reached('after_violation_analysis'):
        return _copy_state(state), scoring, debug

    workload_rows = {
        row['physician_id']: row for row in scoring.get('workload_score_rows', [])
    }
    ranked = sorted(
        physicians,
        key=lambda physician: (
            penalty_by_physician[physician.id],
            Decimal(str(workload_rows.get(physician.id, {}).get('assigned_hours', 0))),
        ),
        reverse=True,
    )
    focus_physician_ids = set(focus_physician_ids or ())
    focus_instance_ids = set(focus_instance_ids or ())
    source_ids = [
        physician.id for physician in ranked
        if physician.id in focus_physician_ids
    ][:max(2, cohort_size // 2)]
    source_ids.extend(
        physician.id for physician in ranked
        if penalty_by_physician[physician.id] > 0
        and physician.id not in source_ids
    )
    source_ids = source_ids[:max(2, cohort_size // 2)]
    if not source_ids:
        return _copy_state(state), scoring, debug
    # Prefer receivers already connected to the violated assignments.  These
    # physicians are more likely to enable a useful exchange chain than an
    # arbitrary low-workload receiver, while the latter remains available as
    # a fallback.  This keeps the neighborhood generic across facilities,
    # contracts, and schedule-block lengths.
    connected_template_ids = {
        instance.shift_template_id
        for instance in instances
        if (focus_start is None or instance.date >= focus_start)
        and (focus_end is None or instance.date <= focus_end)
        and (
            instance.id in focus_instance_ids
            or any(pid in source_ids for pid in state.get(instance.id, ()))
        )
    }
    connected_facility_ids = {
        instance.facility_id
        for instance in instances
        if (
            instance.id in focus_instance_ids
            or instance.shift_template_id in connected_template_ids
        )
    }
    connected_owner_ids = {
        pid
        for instance in instances
        if instance.shift_template_id in connected_template_ids
        for pid in state.get(instance.id, ())
        if pid not in source_ids
    }
    receiver_pool = sorted(
        (
            p for p in physicians
            if p.id not in source_ids
            and eligible_facilities_by_physician.get(p.id)
        ),
        key=lambda physician: (
            0 if physician.id in connected_owner_ids else 1,
            0 if connected_facility_ids.intersection(
                eligible_facilities_by_physician.get(physician.id, set())
            ) else 1,
            0 if workload_rows.get(physician.id, {}).get('deviation_direction') == 'below_minimum' else 1,
            Decimal(str(workload_rows.get(physician.id, {}).get('assigned_hours', 0))),
            physician.id,
        ),
    )[:max(cohort_size * 2, cohort_size)]
    # Sample from a strong receiver pool so repeated neighborhoods do not use
    # the same physicians merely because their current hours sort first.
    receiver_count = max(cohort_size - len(source_ids), 0)
    priority_receivers = [
        physician for physician in receiver_pool
        if physician.id in connected_owner_ids
    ]
    receivers = priority_receivers[:receiver_count]
    remaining_receiver_count = receiver_count - len(receivers)
    selected_receiver_ids = {physician.id for physician in receivers}
    remaining_receiver_pool = [
        physician for physician in receiver_pool
        if physician.id not in selected_receiver_ids
    ]
    if remaining_receiver_count > 0 and remaining_receiver_pool:
        receivers.extend(rng.sample(
            remaining_receiver_pool,
            min(remaining_receiver_count, len(remaining_receiver_pool)),
        ))
    cohort_ids = set(source_ids).union(p.id for p in receivers)
    debug['physician_ids'] = sorted(cohort_ids)
    debug['focus_physician_ids'] = sorted(focus_physician_ids)
    debug['focus_instance_ids'] = sorted(focus_instance_ids)
    debug['connected_owner_ids'] = sorted(connected_owner_ids)
    debug['focus_start'] = focus_start.isoformat() if focus_start else None
    debug['focus_end'] = focus_end.isoformat() if focus_end else None

    instances_by_id = {instance.id: instance for instance in instances}
    movable = []
    for instance in instances:
        if deadline_reached('collect_movable_assignments'):
            return _copy_state(state), scoring, debug
        if focus_start is not None and instance.date < focus_start:
            continue
        if focus_end is not None and instance.date > focus_end:
            continue
        for physician_id in state[instance.id]:
            if physician_id in cohort_ids and (instance.id, physician_id) not in manual_pairs:
                movable.append((instance, physician_id))
    if len(movable) < 2 or len(movable) > 350:
        debug['skipped_reason'] = 'neighborhood_size'
        return _copy_state(state), scoring, debug

    movable_pairs = {(instance.id, owner_id) for instance, owner_id in movable}
    external_indexes = {physician_id: _AssignmentIntervalIndex() for physician_id in cohort_ids}
    for instance in instances:
        if deadline_reached('index_fixed_assignments'):
            return _copy_state(state), scoring, debug
        for physician_id in state[instance.id]:
            if physician_id in cohort_ids and (instance.id, physician_id) not in movable_pairs:
                external_indexes[physician_id].append(
                    (instance.start_datetime, instance.end_datetime)
                )

    model = cp_model.CpModel()
    variables = {}
    slot_candidates = defaultdict(list)
    request_assignment_terms = []
    for slot, (instance, owner_id) in enumerate(movable):
        if deadline_reached('create_assignment_variables'):
            return _copy_state(state), scoring, debug
        for physician_id in cohort_ids:
            if instance.facility_id not in eligible_facilities_by_physician.get(physician_id, set()):
                continue
            if external_indexes[physician_id].conflicts(
                instance, minimum_rest_by_physician[physician_id],
            ):
                continue
            variable = model.new_bool_var(f'x_{slot}_{physician_id}')
            variables[slot, physician_id] = variable
            slot_candidates[slot].append(variable)
            contract = contract_by_physician.get(physician_id)
            if contract is not None:
                assignment_request_score, _violations, _rewards = _request_score(
                    _requests_for_shift(
                        requests_by_physician_date, physician_id, instance,
                    ),
                    contract,
                )
                if assignment_request_score:
                    request_assignment_terms.append(
                        int(Decimal(assignment_request_score) * 60) * variable
                    )
        if not slot_candidates[slot]:
            debug['skipped_reason'] = 'no_candidate_for_slot'
            return _copy_state(state), scoring, debug
        model.add_exactly_one(slot_candidates[slot])

    slots_by_instance = defaultdict(list)
    for slot, (instance, _owner_id) in enumerate(movable):
        slots_by_instance[instance.id].append(slot)
    for slots in slots_by_instance.values():
        for physician_id in cohort_ids:
            same_instance = [
                variables[slot, physician_id]
                for slot in slots if (slot, physician_id) in variables
            ]
            if len(same_instance) > 1:
                model.add_at_most_one(same_instance)

    for left_slot, (left, _left_owner) in enumerate(movable):
        if deadline_reached('create_pair_constraints'):
            return _copy_state(state), scoring, debug
        for right_slot in range(left_slot + 1, len(movable)):
            right = movable[right_slot][0]
            for physician_id in cohort_ids:
                left_var = variables.get((left_slot, physician_id))
                right_var = variables.get((right_slot, physician_id))
                if left_var is None or right_var is None:
                    continue
                index = _AssignmentIntervalIndex()
                index.append((left.start_datetime, left.end_datetime))
                if index.conflicts(right, minimum_rest_by_physician[physician_id]):
                    model.add(left_var + right_var <= 1)

    fixed_by_physician = defaultdict(list)
    for instance in instances:
        for physician_id in state[instance.id]:
            if physician_id in cohort_ids and (instance.id, physician_id) not in movable_pairs:
                fixed_by_physician[physician_id].append(instance)

    # Reusable assignment expressions for the remaining configured rule
    # families.  A physician may have more than one eligible slot for a date
    # or template occurrence, so model the OR explicitly instead of assuming
    # one generated shift per day.
    movable_vars_by_physician_date = defaultdict(list)
    movable_vars_by_physician_instance = defaultdict(list)
    for slot, (instance, _owner_id) in enumerate(movable):
        for physician_id in cohort_ids:
            variable = variables.get((slot, physician_id))
            if variable is None:
                continue
            movable_vars_by_physician_date[physician_id, instance.date].append(variable)
            movable_vars_by_physician_instance[
                physician_id, instance.id
            ].append(variable)

    all_dates = sorted({instance.date for instance in instances})
    worked_by_physician_date = {}
    non_night_by_physician_date = {}
    for physician_id in cohort_ids:
        if deadline_reached('create_daily_assignment_variables'):
            return _copy_state(state), scoring, debug
        fixed_dates = {instance.date for instance in fixed_by_physician[physician_id]}
        fixed_non_night_dates = {
            instance.date
            for instance in fixed_by_physician[physician_id]
            if not instance.shift_template.night_shift
        }
        for day in all_dates:
            date_vars = movable_vars_by_physician_date.get((physician_id, day), [])
            if day in fixed_dates:
                worked_by_physician_date[physician_id, day] = 1
            elif not date_vars:
                worked_by_physician_date[physician_id, day] = 0
            else:
                worked = model.new_bool_var(f'worked_{physician_id}_{day.isoformat()}')
                for variable in date_vars:
                    model.add(worked >= variable)
                model.add(worked <= sum(date_vars))
                worked_by_physician_date[physician_id, day] = worked

            non_night_vars = [
                variable
                for slot, (instance, _owner_id) in enumerate(movable)
                if instance.date == day
                and not instance.shift_template.night_shift
                and (variable := variables.get((slot, physician_id))) is not None
            ]
            if day in fixed_non_night_dates:
                non_night_by_physician_date[physician_id, day] = 1
            elif not non_night_vars:
                non_night_by_physician_date[physician_id, day] = 0
            else:
                non_night = model.new_bool_var(
                    f'non_night_{physician_id}_{day.isoformat()}'
                )
                for variable in non_night_vars:
                    model.add(non_night >= variable)
                model.add(non_night <= sum(non_night_vars))
                non_night_by_physician_date[physician_id, day] = non_night

    def model_or(values, name):
        if any(isinstance(value, int) and value == 1 for value in values):
            return 1
        variables_only = [value for value in values if not isinstance(value, int)]
        if not variables_only:
            return 0
        result = model.new_bool_var(name)
        for value in variables_only:
            model.add(result >= value)
        model.add(result <= sum(variables_only))
        return result

    def model_and(left, right, name):
        if isinstance(left, int):
            return right if left else 0
        if isinstance(right, int):
            return left if right else 0
        result = model.new_bool_var(name)
        model.add(result <= left)
        model.add(result <= right)
        model.add(result >= left + right - 1)
        return result

    objective_terms = list(request_assignment_terms)
    # DAY_ON and SHIFT_ON are scored for absence, not per assignment. Model
    # their satisfaction explicitly so the neighborhood cannot trade away a
    # high-priority requested shift merely to repair workload.
    for (physician_id, request_date), requests in requests_by_physician_date.items():
        if deadline_reached('create_request_constraints'):
            return _copy_state(state), scoring, debug
        if physician_id not in cohort_ids:
            continue
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        for request in requests:
            if request.request_type not in {
                ScheduleRequest.RequestType.DAY_ON,
                ScheduleRequest.RequestType.SHIFT_ON,
            }:
                continue
            template_ids = (
                {template.id for template in request.shift_templates.all()}
                if request.request_type == ScheduleRequest.RequestType.SHIFT_ON
                else None
            )
            fixed_satisfied = any(
                instance.date == request_date
                and (template_ids is None or instance.shift_template_id in template_ids)
                for instance in fixed_by_physician[physician_id]
            )
            if fixed_satisfied:
                continue
            satisfying = [
                variables[slot, physician_id]
                for slot, (instance, _owner_id) in enumerate(movable)
                if (slot, physician_id) in variables
                and instance.date == request_date
                and (template_ids is None or instance.shift_template_id in template_ids)
            ]
            if not satisfying:
                continue
            satisfied = model.new_bool_var(
                f'request_{request.id}_{physician_id}_satisfied'
            )
            model.add(sum(satisfying) >= satisfied)
            model.add(sum(satisfying) <= len(satisfying) * satisfied)
            currently_satisfied = any(
                physician_id in state[instance.id]
                and instance.date == request_date
                and (template_ids is None or instance.shift_template_id in template_ids)
                for instance in instances
            )
            if (
                request.weight == ScheduleRequest.Weight.FIXED
                and currently_satisfied
            ):
                model.add(satisfied == 1)
            unmet = model.new_bool_var(f'request_{request.id}_{physician_id}_unmet')
            model.add(unmet + satisfied == 1)
            objective_terms.append(
                int(Decimal(_request_weight(contract, request.weight)) * 60) * unmet
            )
    # Model the configured workload windows exactly in integer minutes/shifts.
    for physician_id in cohort_ids:
        if deadline_reached('create_workload_constraints'):
            return _copy_state(state), scoring, debug
        for rule in (targets.get(physician_id) or {}).get('rules') or []:
            for window_start, window_end in _period_windows(instances, rule['period_type']):
                effective = _effective_workload_rule(rule, window_start, window_end)
                is_shifts = effective['units'] == 'SHIFTS'
                scale = 1 if is_shifts else 60
                fixed_value = sum(
                    1 if is_shifts else int(round(float(_shift_hours(instance) * 60)))
                    for instance in fixed_by_physician[physician_id]
                    if window_start <= instance.date <= window_end
                )
                terms = []
                for slot, (instance, _owner_id) in enumerate(movable):
                    variable = variables.get((slot, physician_id))
                    if variable is not None and window_start <= instance.date <= window_end:
                        value = 1 if is_shifts else int(round(float(_shift_hours(instance) * 60)))
                        terms.append(value * variable)
                total = fixed_value + sum(terms)
                maximum_possible = fixed_value + sum(
                    1 if is_shifts else int(round(float(_shift_hours(instance) * 60)))
                    for slot, (instance, _owner_id) in enumerate(movable)
                    if (slot, physician_id) in variables
                    and window_start <= instance.date <= window_end
                )
                if effective['min_value'] is not None:
                    under = model.new_int_var(0, max(maximum_possible, 1), 'workload_under')
                    model.add(under >= int(round(float(effective['min_value']) * scale)) - total)
                    objective_terms.append(max(1, int(effective['min_penalty_weight'])) * under)
                if effective['max_value'] is not None:
                    over = model.new_int_var(0, max(maximum_possible, 1), 'workload_over')
                    model.add(over >= total - int(round(float(effective['max_value']) * scale)))
                    objective_terms.append(max(1, int(effective['max_penalty_weight'])) * over)

    # Contract shift-group ranges use the same week/month/schedule-block
    # windows and hour/shift units as the authoritative shift-rule scorer.
    for physician_id in cohort_ids:
        if deadline_reached('create_shift_group_constraints'):
            return _copy_state(state), scoring, debug
        contract = contract_by_physician.get(physician_id)
        raw_settings = getattr(contract, 'shift_settings', {}) if contract is not None else {}
        settings = raw_settings if isinstance(raw_settings, dict) else {}
        for group_index, group in enumerate(settings.get('rules') or []):
            if not isinstance(group, dict):
                continue
            template_ids = {
                int(template_id)
                for template_id in (group.get('shift_template_ids') or [])
                if str(template_id).isdigit()
            }
            if not template_ids:
                continue
            for rule in group.get('period_rules') or []:
                if not isinstance(rule, dict):
                    continue
                period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
                units = 'SHIFTS' if rule.get('units') == 'SHIFTS' else 'HOURS'
                normalized = {
                    'period_type': period_type,
                    'units': units,
                    'min_value': _decimal_or_none(rule.get('min_value')),
                    'max_value': _decimal_or_none(rule.get('max_value')),
                    'min_penalty_weight': _positive_decimal_or_default(
                        rule.get('min_penalty_weight'), Decimal('0'),
                    ),
                    'max_penalty_weight': _positive_decimal_or_default(
                        rule.get('max_penalty_weight'), Decimal('0'),
                    ),
                }
                if normalized['min_value'] is None and normalized['max_value'] is None:
                    continue
                for window_start, window_end in _period_windows(instances, period_type):
                    effective = _effective_workload_rule(
                        normalized, window_start, window_end,
                    )
                    is_shifts = units == 'SHIFTS'
                    scale = 1 if is_shifts else 60
                    fixed_value = sum(
                        1 if is_shifts else int(round(float(_shift_hours(instance) * 60)))
                        for instance in fixed_by_physician[physician_id]
                        if instance.shift_template_id in template_ids
                        and window_start <= instance.date <= window_end
                    )
                    terms = []
                    for slot, (instance, _owner_id) in enumerate(movable):
                        variable = variables.get((slot, physician_id))
                        if (
                            variable is not None
                            and instance.shift_template_id in template_ids
                            and window_start <= instance.date <= window_end
                        ):
                            value = (
                                1 if is_shifts
                                else int(round(float(_shift_hours(instance) * 60)))
                            )
                            terms.append(value * variable)
                    total = fixed_value + sum(terms)
                    maximum_possible = fixed_value + sum(
                        1 if is_shifts else int(round(float(_shift_hours(instance) * 60)))
                        for slot, (instance, _owner_id) in enumerate(movable)
                        if (slot, physician_id) in variables
                        and instance.shift_template_id in template_ids
                        and window_start <= instance.date <= window_end
                    )
                    if effective['min_value'] is not None:
                        under = model.new_int_var(
                            0, max(maximum_possible, int(effective['min_value'] * scale), 1),
                            f'shift_group_under_{physician_id}_{group_index}_{window_start.isoformat()}',
                        )
                        model.add(
                            under >= int(round(float(effective['min_value']) * scale)) - total
                        )
                        objective_terms.append(
                            max(1, int(effective['min_penalty_weight'])) * under
                        )
                    if effective['max_value'] is not None:
                        over = model.new_int_var(
                            0, max(maximum_possible, int(effective['max_value'] * scale), 1),
                            f'shift_group_over_{physician_id}_{group_index}_{window_start.isoformat()}',
                        )
                        model.add(
                            over >= total - int(round(float(effective['max_value']) * scale))
                        )
                        objective_terms.append(
                            max(1, int(effective['max_penalty_weight'])) * over
                        )

    # Weekend volume is part of the authoritative score and must participate
    # in reconstruction.  Previously, a CP candidate could cheaply fix a
    # workload or request violation by creating a much larger weekend cost.
    for physician_id in cohort_ids:
        if deadline_reached('create_weekend_constraints'):
            return _copy_state(state), scoring, debug
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        settings = (
            contract.weekend_settings
            if isinstance(contract.weekend_settings, dict) else {}
        )
        for rule in settings.get('period_rules') or []:
            if not isinstance(rule, dict):
                continue
            period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
            for window_start, window_end in _period_windows(instances, period_type):
                fixed_count = sum(
                    1 for instance in fixed_by_physician[physician_id]
                    if window_start <= instance.date <= window_end
                    and instance.date.strftime('%A') in (
                        instance.shift_template.weekend_days or []
                    )
                )
                terms = []
                for slot, (instance, _owner_id) in enumerate(movable):
                    variable = variables.get((slot, physician_id))
                    if (
                        variable is not None
                        and window_start <= instance.date <= window_end
                        and instance.date.strftime('%A') in (
                            instance.shift_template.weekend_days or []
                        )
                    ):
                        terms.append(variable)
                total = fixed_count + sum(terms)
                maximum_possible = fixed_count + len(terms)
                for side in ('min', 'max'):
                    limit = _decimal_or_none(rule.get(f'{side}_volume'))
                    weight = _decimal_or_none(rule.get(f'{side}_penalty_weight'))
                    if limit is None or weight is None or weight <= 0:
                        continue
                    deviation = model.new_int_var(
                        0, max(maximum_possible, int(limit), 1),
                        f'weekend_{side}_{physician_id}_{window_start.isoformat()}',
                    )
                    if side == 'min':
                        model.add(deviation >= int(limit) - total)
                    else:
                        model.add(deviation >= total - int(limit))
                    objective_terms.append(int(weight * 60) * deviation)

        weekend_by_date = {}
        fixed_weekend_dates = {
            instance.date
            for instance in fixed_by_physician[physician_id]
            if instance.date.strftime('%A') in (
                instance.shift_template.weekend_days or []
            )
        }
        for day in all_dates:
            if day in fixed_weekend_dates:
                weekend_by_date[day] = 1
                continue
            values = [
                variables[slot, physician_id]
                for slot, (instance, _owner_id) in enumerate(movable)
                if (slot, physician_id) in variables
                and instance.date == day
                and day.strftime('%A') in (
                    instance.shift_template.weekend_days or []
                )
            ]
            weekend_by_date[day] = model_or(
                values,
                f'weekend_day_{physician_id}_{day.isoformat()}',
            )

        min_weekend_shifts = _configured_positive_int(
            settings, 'min_consecutive_weekend_shifts',
        )
        min_weekend_shift_weight = _configured_positive_penalty(
            settings,
            'min_consecutive_weekend_shifts_penalty_weight',
            Decimal('0'),
        )
        max_weekend_shifts = _configured_positive_int(
            settings, 'max_consecutive_weekend_shifts',
        )
        max_weekend_shift_weight = _configured_positive_penalty(
            settings,
            'max_consecutive_weekend_shifts_penalty_weight',
            Decimal('0'),
        )
        if min_weekend_shifts and min_weekend_shift_weight > 0:
            for day in all_dates:
                current = weekend_by_date.get(day, 0)
                if isinstance(current, int) and current == 0:
                    continue
                previous = weekend_by_date.get(day - timedelta(days=1), 0)
                block_start = model_and(
                    current,
                    1 - previous,
                    f'weekend_shift_start_{physician_id}_{day.isoformat()}',
                )
                for offset in range(1, min_weekend_shifts):
                    future = weekend_by_date.get(day + timedelta(days=offset), 0)
                    missing = model_and(
                        block_start,
                        1 - future,
                        f'weekend_shift_short_{physician_id}_{day.isoformat()}_{offset}',
                    )
                    objective_terms.append(
                        int(min_weekend_shift_weight * 60) * missing
                    )
        if max_weekend_shifts and max_weekend_shift_weight > 0:
            for start_index in range(max(len(all_dates) - max_weekend_shifts, 0)):
                window = all_dates[start_index:start_index + max_weekend_shifts + 1]
                if not all(
                    next_day == day + timedelta(days=1)
                    for day, next_day in zip(window, window[1:])
                ):
                    continue
                values = [weekend_by_date.get(day, 0) for day in window]
                if any(isinstance(value, int) and value == 0 for value in values):
                    continue
                if all(isinstance(value, int) and value == 1 for value in values):
                    objective_terms.append(int(max_weekend_shift_weight * 60))
                    continue
                excess = model.new_bool_var(
                    f'weekend_shift_excess_{physician_id}_{start_index}'
                )
                for value in values:
                    model.add(excess <= value)
                model.add(excess >= sum(values) - len(values) + 1)
                objective_terms.append(int(max_weekend_shift_weight * 60) * excess)

        weekend_starts = sorted({
            day - timedelta(days=day.weekday())
            for day in all_dates
        })
        worked_weekend = {
            weekend_start: model_or(
                [
                    weekend_by_date.get(weekend_start + timedelta(days=offset), 0)
                    for offset in range(7)
                ],
                f'worked_weekend_{physician_id}_{weekend_start.isoformat()}',
            )
            for weekend_start in weekend_starts
        }
        min_weekends = _configured_positive_int(
            settings, 'min_consecutive_weekends',
        )
        min_weekends_weight = _configured_positive_penalty(
            settings, 'min_consecutive_weekends_penalty_weight', Decimal('0'),
        )
        max_weekends = _configured_positive_int(
            settings, 'max_consecutive_weekends',
        )
        max_weekends_weight = _configured_positive_penalty(
            settings, 'max_consecutive_weekends_penalty_weight', Decimal('0'),
        )
        if min_weekends and min_weekends_weight > 0:
            for weekend_start in weekend_starts:
                current = worked_weekend[weekend_start]
                if isinstance(current, int) and current == 0:
                    continue
                previous = worked_weekend.get(
                    weekend_start - timedelta(days=7), 0,
                )
                streak_start = model_and(
                    current,
                    1 - previous,
                    f'weekend_streak_start_{physician_id}_{weekend_start.isoformat()}',
                )
                for offset in range(1, min_weekends):
                    future = worked_weekend.get(
                        weekend_start + timedelta(days=7 * offset), 0,
                    )
                    missing = model_and(
                        streak_start,
                        1 - future,
                        f'weekend_streak_short_{physician_id}_{weekend_start.isoformat()}_{offset}',
                    )
                    objective_terms.append(int(min_weekends_weight * 60) * missing)
        if max_weekends and max_weekends_weight > 0:
            for start_index in range(max(len(weekend_starts) - max_weekends, 0)):
                window = weekend_starts[start_index:start_index + max_weekends + 1]
                if not all(
                    next_start == start + timedelta(days=7)
                    for start, next_start in zip(window, window[1:])
                ):
                    continue
                values = [worked_weekend[start] for start in window]
                if any(isinstance(value, int) and value == 0 for value in values):
                    continue
                if all(isinstance(value, int) and value == 1 for value in values):
                    objective_terms.append(int(max_weekends_weight * 60))
                    continue
                excess = model.new_bool_var(
                    f'weekend_streak_excess_{physician_id}_{start_index}'
                )
                for value in values:
                    model.add(excess <= value)
                model.add(excess >= sum(values) - len(values) + 1)
                objective_terms.append(int(max_weekends_weight * 60) * excess)

        friday_weight = _configured_positive_penalty(
            settings,
            'block_friday_night_before_weekend_off_penalty_weight',
            Decimal('0'),
        )
        if settings.get('block_friday_night_before_weekend_off') and friday_weight > 0:
            block_start = min(all_dates) if all_dates else None
            block_end = max(all_dates) if all_dates else None
            fixed_friday_nights = {
                instance.date
                for instance in fixed_by_physician[physician_id]
                if instance.date.weekday() == 4
                and instance.shift_template.night_shift
            }
            for friday in (day for day in all_dates if day.weekday() == 4):
                saturday = friday + timedelta(days=1)
                sunday = friday + timedelta(days=2)
                if block_start is None or friday < block_start or sunday > block_end:
                    continue
                friday_night = (
                    1 if friday in fixed_friday_nights else model_or(
                        [
                            variables[slot, physician_id]
                            for slot, (instance, _owner_id) in enumerate(movable)
                            if (slot, physician_id) in variables
                            and instance.date == friday
                            and instance.shift_template.night_shift
                        ],
                        f'friday_night_{physician_id}_{friday.isoformat()}',
                    )
                )
                weekend_work = model_or(
                    [
                        weekend_by_date.get(saturday, 0),
                        weekend_by_date.get(sunday, 0),
                    ],
                    f'weekend_work_{physician_id}_{friday.isoformat()}',
                )
                violation = model_and(
                    friday_night,
                    1 - weekend_work,
                    f'friday_before_weekend_off_{physician_id}_{friday.isoformat()}',
                )
                objective_terms.append(int(friday_weight * 60) * violation)

    # Exact excess-day formulation: each fully worked window of max+1
    # consecutive dates contributes one configured penalty, matching
    # _streak_excess_score for longer streaks as overlapping windows.
    for physician_id in cohort_ids:
        if deadline_reached('create_consecutive_day_constraints'):
            return _copy_state(state), scoring, debug
        contract = contract_by_physician.get(physician_id)
        if contract is None:
            continue
        workload_settings = (
            contract.workload_settings
            if isinstance(contract.workload_settings, dict) else {}
        )
        max_days = _configured_positive_int(workload_settings, 'max_days_in_row')
        max_days_penalty = _configured_positive_penalty(
            workload_settings, 'max_days_in_row_penalty_weight', Decimal('0'),
        )
        if max_days is None or max_days_penalty <= 0:
            continue
        for start_index in range(max(len(all_dates) - max_days, 0)):
            window = all_dates[start_index:start_index + max_days + 1]
            if not all(
                next_day == day + timedelta(days=1)
                for day, next_day in zip(window, window[1:])
            ):
                continue
            values = [worked_by_physician_date[physician_id, day] for day in window]
            if all(isinstance(value, int) and value == 1 for value in values):
                objective_terms.append(int(max_days_penalty * 60))
                continue
            if any(isinstance(value, int) and value == 0 for value in values):
                continue
            excess = model.new_bool_var(
                f'consecutive_day_excess_{physician_id}_{start_index}'
            )
            for value in values:
                model.add(excess <= value)
            model.add(excess >= sum(values) - len(values) + 1)
            objective_terms.append(int(max_days_penalty * 60) * excess)

    # Same-template streak penalties use occurrence order rather than calendar
    # adjacency. This mirrors the authoritative report and remains valid for
    # templates that do not occur every day.
    instances_by_template = _instances_by_template(instances)
    for physician_id in cohort_ids:
        if deadline_reached('create_same_shift_constraints'):
            return _copy_state(state), scoring, debug
        contract = contract_by_physician.get(physician_id)
        same_shift_rule = _same_shift_rule(contract) if contract is not None else None
        if same_shift_rule is None:
            continue
        max_streak, penalty = same_shift_rule
        fixed_ids = {instance.id for instance in fixed_by_physician[physician_id]}
        for shift_template_id, template_instances in instances_by_template.items():
            assigned = []
            for instance in template_instances:
                if instance.id in fixed_ids:
                    assigned.append(1)
                    continue
                instance_vars = movable_vars_by_physician_instance.get(
                    (physician_id, instance.id), []
                )
                assigned.append(sum(instance_vars) if instance_vars else 0)
            for start_index in range(max(len(assigned) - max_streak, 0)):
                values = assigned[start_index:start_index + max_streak + 1]
                if any(isinstance(value, int) and value == 0 for value in values):
                    continue
                if all(isinstance(value, int) and value == 1 for value in values):
                    objective_terms.append(int(penalty * 60))
                    continue
                excess = model.new_bool_var(
                    f'same_shift_excess_{physician_id}_{shift_template_id}_{start_index}'
                )
                for value in values:
                    model.add(excess <= value)
                model.add(excess >= sum(values) - len(values) + 1)
                objective_terms.append(int(penalty * 60) * excess)

    # A block-start proxy lets the solver move several nights together instead
    # of producing the isolated-night states that trap pairwise hill climbing.
    night_slots_by_physician_date = defaultdict(list)
    fixed_night_dates = defaultdict(set)
    for physician_id, fixed_instances in fixed_by_physician.items():
        fixed_night_dates[physician_id] = {
            instance.date for instance in fixed_instances if instance.shift_template.night_shift
        }
    all_dates = sorted({instance.date for instance in instances})

    for slot, (instance, _owner_id) in enumerate(movable):
        if instance.shift_template.night_shift:
            for physician_id in cohort_ids:
                if (slot, physician_id) in variables:
                    night_slots_by_physician_date[physician_id, instance.date].append(
                        variables[slot, physician_id]
                    )
    for physician_id in cohort_ids:
        if deadline_reached('create_night_constraints'):
            return _copy_state(state), scoring, debug
        night_by_date = {}
        for day in all_dates:
            if day in fixed_night_dates[physician_id]:
                night_by_date[day] = 1
                continue
            date_vars = night_slots_by_physician_date.get((physician_id, day), [])
            if not date_vars:
                night_by_date[day] = 0
                continue
            night = model.new_bool_var(f'night_{physician_id}_{day.isoformat()}')
            model.add(night == sum(date_vars))
            night_by_date[day] = night
        contract = contract_by_physician.get(physician_id)
        settings = _night_settings(contract) if contract is not None else {}
        # Configured night-volume rules, including deduplication of equivalent
        # minimum rules, mirror the full scorer's period windows.
        seen_minimums = set()
        for rule in _unique_night_period_rules(settings):
            period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
            for window_start, window_end in _period_windows(instances, period_type):
                fixed_count = sum(
                    1 for instance in fixed_by_physician[physician_id]
                    if instance.shift_template.night_shift
                    and window_start <= instance.date <= window_end
                )
                terms = [
                    variable
                    for slot, (instance, _owner_id) in enumerate(movable)
                    if instance.shift_template.night_shift
                    and window_start <= instance.date <= window_end
                    and (variable := variables.get((slot, physician_id))) is not None
                ]
                total = fixed_count + sum(terms)
                maximum_possible = fixed_count + len(terms)
                min_shifts = _decimal_or_none(rule.get('min_shifts'))
                min_weight = _decimal_or_none(rule.get('min_penalty_weight'))
                minimum_key = (window_start, window_end, min_shifts)
                if (
                    min_shifts is not None and min_shifts > 0
                    and min_weight is not None and min_weight > 0
                    and minimum_key not in seen_minimums
                ):
                    seen_minimums.add(minimum_key)
                    under = model.new_int_var(
                        0, max(maximum_possible, int(min_shifts), 1),
                        f'night_under_{physician_id}_{window_start.isoformat()}',
                    )
                    model.add(under >= int(min_shifts) - total)
                    objective_terms.append(int(min_weight * 60) * under)
                max_shifts = _decimal_or_none(rule.get('max_shifts'))
                max_weight = _decimal_or_none(rule.get('max_penalty_weight'))
                if (
                    max_shifts is not None and max_shifts >= 0
                    and max_weight is not None and max_weight > 0
                ):
                    over = model.new_int_var(
                        0, max(maximum_possible, int(max_shifts), 1),
                        f'night_over_{physician_id}_{window_start.isoformat()}',
                    )
                    model.add(over >= total - int(max_shifts))
                    objective_terms.append(int(max_weight * 60) * over)
        min_consecutive = _configured_positive_int(
            settings, 'min_consecutive_night_shifts',
        )
        max_consecutive = _configured_positive_int(
            settings, 'max_consecutive_night_shifts',
        )
        max_consecutive_penalty = _configured_positive_penalty(
            settings,
            'max_consecutive_night_shifts_penalty_weight',
            DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
        )
        for start_index in range(
            0,
            max(len(all_dates) - max_consecutive, 0) if max_consecutive else 0,
        ):
            window = all_dates[start_index:start_index + max_consecutive + 1]
            if all(
                next_day == day + timedelta(days=1)
                for day, next_day in zip(window, window[1:])
            ):
                excess = model.new_bool_var(
                    f'night_excess_{physician_id}_{start_index}'
                )
                model.add(
                    excess >= sum(night_by_date[day] for day in window) - max_consecutive
                )
                objective_terms.append(
                    int(max_consecutive_penalty * 60) * excess
                )
        block_starts = {}
        block_ends = {}
        for index, day in enumerate(all_dates):
            current_night = night_by_date[day]
            if isinstance(current_night, int) and current_night == 0:
                continue
            previous = night_by_date.get(day - timedelta(days=1), 0)
            block_start = model.new_bool_var(f'block_start_{physician_id}_{index}')
            model.add(block_start >= current_night - previous)
            model.add(block_start <= current_night)
            if isinstance(previous, int):
                if previous == 1:
                    model.add(block_start == 0)
            else:
                model.add(block_start <= 1 - previous)
            block_starts[day] = block_start
            following = night_by_date.get(day + timedelta(days=1), 0)
            block_end = model.new_bool_var(f'block_end_{physician_id}_{index}')
            model.add(block_end >= current_night - following)
            model.add(block_end <= current_night)
            if isinstance(following, int):
                if following == 1:
                    model.add(block_end == 0)
            else:
                model.add(block_end <= 1 - following)
            block_ends[day] = block_end
        if min_consecutive and min_consecutive > 1:
            min_consecutive_penalty = _configured_positive_penalty(
                settings,
                'min_consecutive_night_shifts_penalty_weight',
                DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
            )
            for day, block_start in block_starts.items():
                for offset in range(1, min_consecutive):
                    future = night_by_date.get(day + timedelta(days=offset), 0)
                    missing = model.new_bool_var(
                        f'night_short_{physician_id}_{day.isoformat()}_{offset}'
                    )
                    model.add(missing >= block_start - future)
                    objective_terms.append(
                        int(min_consecutive_penalty * 60) * missing
                    )

        # Model recovery penalties from the end of one night block. Summed
        # threshold indicators reproduce the shortfall charged by the full
        # scorer: the earlier the next assignment/block, the larger the cost.
        days_after = _configured_positive_int(settings, 'days_off_after_night_block')
        days_after_weight = _configured_positive_penalty(
            settings, 'days_off_after_night_block_penalty_weight', Decimal('0'),
        )
        if days_after is not None and days_after_weight > 0:
            for day, block_end in block_ends.items():
                for threshold in range(1, days_after + 1):
                    nearby_non_night = model_or(
                        [
                            non_night_by_physician_date.get(
                                (physician_id, day + timedelta(days=offset)), 0,
                            )
                            for offset in range(1, threshold + 1)
                        ],
                        f'non_night_within_{physician_id}_{day.isoformat()}_{threshold}',
                    )
                    violation = model_and(
                        block_end, nearby_non_night,
                        f'night_recovery_{physician_id}_{day.isoformat()}_{threshold}',
                    )
                    objective_terms.append(int(days_after_weight * 60) * violation)

        days_before = _configured_positive_int(
            settings, 'days_off_before_next_night_shift',
        )
        days_before_weight = _configured_positive_penalty(
            settings, 'days_off_before_next_night_shift_penalty_weight', Decimal('0'),
        )
        if days_before is not None and days_before_weight > 0:
            for day, block_end in block_ends.items():
                for threshold in range(2, days_before + 1):
                    nearby_block_start = model_or(
                        [
                            block_starts.get(day + timedelta(days=offset), 0)
                            for offset in range(2, threshold + 1)
                        ],
                        f'next_night_within_{physician_id}_{day.isoformat()}_{threshold}',
                    )
                    violation = model_and(
                        block_end, nearby_block_start,
                        f'next_night_recovery_{physician_id}_{day.isoformat()}_{threshold}',
                    )
                    objective_terms.append(int(days_before_weight * 60) * violation)

    # Ordinary repair prefers assignment stability. During a diversified
    # restart, signed tie weights deliberately produce a different solution
    # among otherwise equivalent neighborhood optima.
    for slot, (_instance, owner_id) in enumerate(movable):
        if deadline_reached('create_stability_objective'):
            return _copy_state(state), scoring, debug
        for physician_id in cohort_ids:
            variable = variables.get((slot, physician_id))
            if variable is not None and physician_id != owner_id:
                tie_weight = (
                    rng.randint(-7, 7)
                    if diversification_bias
                    else rng.randint(1, 7)
                )
                objective_terms.append(tie_weight * variable)
    model.minimize(sum(objective_terms))

    remaining_seconds = neighborhood_deadline - clock()
    if should_stop() or remaining_seconds < 0.1:
        debug['stopped'] = True
        debug['stopped_stage'] = 'before_solver'
        return _copy_state(state), scoring, debug

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(
        min(float(time_limit_seconds), remaining_seconds), 0.1,
    )
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = rng.randrange(1, 2_000_000_000)
    status = solver.solve(model)
    debug['attempts'] = 1
    debug['solver_status'] = solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _copy_state(state), scoring, debug

    trial = _copy_state(state)
    for instance, owner_id in movable:
        if owner_id in trial[instance.id]:
            trial[instance.id].remove(owner_id)
    for slot, (instance, _owner_id) in enumerate(movable):
        for physician_id in cohort_ids:
            variable = variables.get((slot, physician_id))
            if variable is not None and solver.boolean_value(variable):
                trial[instance.id].append(physician_id)
                break
    trial_scoring = _score_schedule(
        instances, physicians, trial, targets, contract_by_physician,
        requests_by_physician_date, eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    debug['candidates'] = 1
    debug['score_before'] = float(scoring['score'])
    debug['score_after'] = float(trial_scoring['score'])
    debug['breakdown_before'] = {
        key: float(value) for key, value in scoring['breakdown'].items()
    }
    debug['breakdown_after'] = {
        key: float(value) for key, value in trial_scoring['breakdown'].items()
    }
    score_increase = trial_scoring['score'] - scoring['score']
    debug['assignment_distance'] = sum(
        len(set(state[instance.id]).symmetric_difference(trial[instance.id]))
        for instance in instances
    ) // 2
    acceptable_increase = (
        maximum_score_increase is None
        or score_increase <= Decimal(str(maximum_score_increase))
    )
    if (
        _optimization_priority(trial_scoring) < _optimization_priority(scoring)
        or (
            allow_non_improving
            and debug['assignment_distance'] > 0
            and acceptable_increase
        )
    ):
        debug['accepted'] = True
        debug['accepted_for_diversification'] = score_increase >= 0
        return trial, trial_scoring, debug
    return _copy_state(state), scoring, debug


def _state_assignment_distance(left_state, right_state):
    """Count assignment ownership changes between two complete states."""
    instance_ids = set(left_state).union(right_state)
    return sum(
        len(
            set(left_state.get(instance_id, ())).symmetric_difference(
                right_state.get(instance_id, ())
            )
        )
        for instance_id in instance_ids
    ) // 2


def _constraint_boundary_padding_days(contract_by_physician, maximum=7):
    """Return bounded context needed around a reconstruction window.

    Temporal rules can be violated just outside a selected date range. Moving
    the boundary assignments as part of the same neighborhood gives CP-SAT a
    chance to repair that interaction instead of merely observing it as fixed
    context. The cap keeps long blocks computationally bounded.
    """
    configured = [1]
    for contract in contract_by_physician.values():
        night_settings = _night_settings(contract)
        workload_settings = (
            contract.workload_settings
            if isinstance(contract.workload_settings, dict) else {}
        )
        for key in (
            'min_consecutive_night_shifts',
            'max_consecutive_night_shifts',
            'days_off_after_night_block',
            'days_off_before_next_night_shift',
        ):
            value = _configured_positive_int(night_settings, key)
            if value is not None:
                configured.append(value)
        max_days = _configured_positive_int(workload_settings, 'max_days_in_row')
        if max_days is not None:
            configured.append(max_days)
    return min(max(configured), maximum)


def _adaptive_violation_focuses(
    *, instances, physicians, state, scoring, contract_by_physician,
    requests_by_physician_date,
):
    """Return weighted physician/date seeds for destroy-and-repair windows.

    These are derived from the authoritative score reports rather than from a
    schedule-block-specific rule.  A restart can therefore target whichever
    configured constraints are active in the current problem.
    """
    dates_by_iso = {instance.date.isoformat(): instance.date for instance in instances}
    focuses = []

    def add_focus(row, penalty=None):
        physician_id = row.get('physician_id')
        if physician_id is None:
            return
        dates = [
            dates_by_iso[value]
            for value in row.get('dates_involved', ())
            if value in dates_by_iso
        ]
        for key in ('period_start', 'period_end'):
            value = row.get(key)
            if value in dates_by_iso:
                dates.append(dates_by_iso[value])
        amount = Decimal(str(
            penalty
            if penalty is not None
            else row.get('penalty_amount', row.get('penalty', 0))
        ))
        if amount <= 0:
            return
        focuses.append({
            'physician_ids': {physician_id},
            'dates': sorted(set(dates)),
            'penalty': amount,
            'violation_type': row.get('violation_type', 'WORKLOAD'),
        })

    for row in scoring.get('workload_score_rows', ()):
        for rule_row in row.get('rule_rows', ()):
            if Decimal(str(rule_row.get('score_contribution', 0))) > 0:
                add_focus(
                    {'physician_id': row['physician_id'], **rule_row},
                    rule_row['score_contribution'],
                )
    for row in scoring.get('same_shift_violations', ()):
        add_focus(row)
    for row in _night_violation_report(
        instances, physicians, state, contract_by_physician,
    ).get('night_violations', ()):
        add_focus(row)
    for row in _weekend_volume_report(
        instances, physicians, state, contract_by_physician, details=True,
    ).get('violations', ()):
        add_focus(row)
    for row in _shift_period_report(
        instances, physicians, state, contract_by_physician, details=True,
    ).get('violations', ()):
        add_focus(row)
    for row in _request_scoring_rows(
        instances, physicians, state, contract_by_physician,
        requests_by_physician_date,
    ):
        add_focus(row)

    return sorted(
        focuses,
        key=lambda focus: (focus['penalty'], len(focus['dates'])),
        reverse=True,
    )


def _adaptive_repair_order(repairs, repair_stats, cycle):
    """Spend continuation time on repairs that are producing global progress.

    Every repair receives an initial trial.  Thereafter, productive repairs are
    ranked by authoritative score reduction per second.  Repeatedly
    unproductive repairs receive an exponentially increasing, bounded
    cooldown instead of consuming the same four-second slice every cycle.
    """
    ordered = []
    for original_index, repair_row in enumerate(repairs):
        repair_name = repair_row[0]
        stats = repair_stats.get(repair_name) or {}
        if int(stats.get('cooldown_until_cycle', 0)) > cycle:
            continue
        calls = int(stats.get('epoch_calls', stats.get('calls', 0)))
        gain = float(
            stats.get('recent_score_improvement', stats.get('score_improvement', 0)) or 0
        )
        runtime = max(float(
            stats.get('recent_runtime_seconds', stats.get('runtime_seconds', 0)) or 0
        ), 0.001)
        if calls == 0:
            category = 0  # Give every strategy one evidence-gathering slice.
            rate = 0.0
        elif gain > 0:
            category = -1
            rate = gain / runtime
        else:
            category = 1
            rate = 0.0
        ordered.append((
            category,
            -rate,
            int(stats.get('consecutive_no_gain_calls', 0)),
            original_index,
            repair_row,
        ))
    return [row[-1] for row in sorted(ordered)]


def _adaptive_repair_slice_seconds(stats):
    """Choose a bounded strategy slice from retained global-score evidence.

    New and productive strategies receive the full slice. Strategies that
    repeatedly fail to improve the retained global best still receive short
    exploratory retries, allowing them to become useful after another repair
    changes the schedule without repeatedly consuming a full slice.
    """
    calls = int(stats.get('epoch_calls', stats.get('calls', 0)) or 0)
    if calls == 0:
        if int(stats.get('consecutive_empty_calls', 0) or 0) >= 2:
            return 1.0
        if int(stats.get('consecutive_no_gain_calls', 0) or 0) >= 2:
            return 1.5
        return 4.0
    recent_gain = float(
        stats.get('recent_score_improvement', stats.get('score_improvement', 0)) or 0
    )
    if recent_gain > 0:
        return 4.0
    no_gain_calls = int(stats.get('consecutive_no_gain_calls', 0) or 0)
    if no_gain_calls >= 4:
        return 1.0
    if no_gain_calls >= 2:
        return 1.5
    return 2.0


def _record_adaptive_repair_productivity(
    stats, *, score_gain, runtime_seconds, cycle, attempts=None,
):
    """Update one repair's ROI and back it off after repeated zero-gain calls."""
    stats['runtime_seconds'] = (
        float(stats.get('runtime_seconds', 0) or 0) + float(runtime_seconds)
    )
    stats['score_improvement'] = float(stats.get('score_improvement', 0) or 0) + float(score_gain)
    stats['recent_runtime_seconds'] = (
        float(stats.get('recent_runtime_seconds', 0) or 0) * 0.7
        + float(runtime_seconds)
    )
    stats['recent_score_improvement'] = (
        float(stats.get('recent_score_improvement', 0) or 0) * 0.7
        + float(score_gain)
    )
    empty_call = attempts == 0
    if empty_call:
        stats['consecutive_empty_calls'] = (
            int(stats.get('consecutive_empty_calls', 0)) + 1
        )
    elif attempts is not None:
        stats['consecutive_empty_calls'] = 0
    if score_gain > 0:
        stats['consecutive_no_gain_calls'] = 0
        stats['cooldown_until_cycle'] = cycle
    else:
        zero_calls = int(stats.get('consecutive_no_gain_calls', 0)) + 1
        stats['consecutive_no_gain_calls'] = zero_calls
        if empty_call:
            # Some repairs spend most of their slice constructing an
            # authoritative report before discovering that the current state
            # contains no move to evaluate. Retrying one after every small
            # best or diversification wastes a large fraction of a long-block
            # run. Keep checking it periodically, but back it off immediately
            # and increasingly while it continues to return no candidates.
            empty_calls = int(stats['consecutive_empty_calls'])
            cooldown_cycles = min(12, 2 ** min(empty_calls + 1, 3))
            stats['cooldown_until_cycle'] = cycle + cooldown_cycles + 1
        elif zero_calls >= 2:
            cooldown_cycles = min(8, 2 ** min(zero_calls - 2, 3))
            stats['cooldown_until_cycle'] = cycle + cooldown_cycles + 1
    total_runtime = max(float(stats.get('recent_runtime_seconds', 0) or 0), 0.001)
    stats['score_improvement_per_second'] = (
        float(stats['recent_score_improvement']) / total_runtime
    )


def _run_adaptive_search_rounds(
    *, instances, physicians, initial_state, initial_scoring, manual_pairs, targets,
    contract_by_physician, requests_by_physician_date,
    eligible_facilities_by_physician, minimum_rest_by_physician,
    search_budget, rng, debug, progress_callback=None,
):
    """Repeat the current continuation neighborhoods while retaining the best valid state."""
    best_state = _copy_state(initial_state)
    best_scoring = initial_scoring
    repairs = []
    if _unfilled_slot_count(instances, initial_state):
        repairs.append(('direct_coverage', _repair_direct_coverage, {}))
    repairs.extend([
        ('weekend_support_swaps', _repair_weekend_support_swaps, {}),
        ('general_reassignments', _repair_general_constraint_reassignments, {}),
        ('general_swaps', _repair_general_constraint_swaps, {}),
        ('workload_transfers', _repair_workload_transfers, {}),
        ('night_minimum', _repair_night_minimum_distribution, {}),
        ('night_maximum', _repair_night_spacing_swaps, {'maximum_only': True}),
        ('night_recovery', _repair_recovery_day_swaps, {}),
        ('night_spacing', _repair_night_spacing_swaps, {}),
    ])
    repair_stats = debug.setdefault('repair_stats', {})
    debug.setdefault('restarts', 0)
    debug.setdefault('proactive_restarts', 0)
    debug.setdefault('best_chain_restarts', 0)
    debug.setdefault('seed_epochs', 0)
    debug.setdefault('pipeline_epoch_restarts', 0)
    debug.setdefault('pipeline_epochs', [])
    debug.setdefault('best_chain_details', [])
    debug.setdefault('restart_details', [])
    exploration_state = _copy_state(best_state)
    instances_by_id = {instance.id: instance for instance in instances}
    locked_open_instance_ids = {instance.id for instance in instances if instance.is_locked_open}
    budget_clock = getattr(search_budget, 'clock', monotonic)
    last_diversification_at = budget_clock()
    pipeline_epoch_number = 1
    pipeline_epoch_started_at = budget_clock()
    pipeline_epoch_start_score = best_scoring['score']
    pipeline_epoch_start_improvements = int(debug.get('improvements', 0))
    pipeline_epoch_seed = None
    pipeline_epoch_kind = 'initial'
    pipeline_epoch_open = True
    consecutive_exhausted_pipeline_epochs = 0
    debug['pipeline_epoch_controller'] = 'productivity_driven'
    debug['pipeline_epoch_seconds'] = None
    debug.setdefault('consecutive_exhausted_pipeline_epochs', 0)
    debug.setdefault('deep_pipeline_epoch_restarts', 0)
    debug.setdefault('deep_pipeline_epoch_details', [])
    debug.setdefault('coupled_weekend_repair_activations', 0)
    cycles_without_global_improvement = 0
    best_generation = 0
    proactive_restart_interval_seconds = min(
        30,
        max(float(getattr(search_budget, 'stall_seconds', 120)) / 4, 10),
    )

    def keep_progress(candidate_state, candidate_scoring):
        nonlocal best_state, best_scoring, best_generation
        candidate_priority = _state_result_priority(
            instances, candidate_state, candidate_scoring,
        )
        valid = (
            candidate_priority[0] == 0
            and all(pid in candidate_state[sid] for sid, pid in manual_pairs)
        )
        search_budget.observe(candidate_scoring['score'], valid=valid)
        if (
            valid
            and candidate_priority
            < _state_result_priority(instances, best_state, best_scoring)
        ):
            best_state = _copy_state(candidate_state)
            best_scoring = candidate_scoring
            best_generation += 1
            debug['improvements'] += 1
            if progress_callback is not None:
                progress_callback(best_scoring['score'])
            return True
        return False

    def diversify_from_best():
        """Leave the local basin with violation-directed destroy and repair.

        The global best remains untouched. The exploration state may become
        temporarily worse within a bounded temperature so subsequent repairs
        can cross a local-score barrier.
        """
        # Keep the entire diversification attempt bounded, not only its
        # constraint-solver phase. Pairwise kick evaluation can be expensive
        # on multi-month blocks because every candidate is fully rescored.
        # Without this shared deadline a single restart could run far beyond
        # the optimizer's absolute SearchBudget deadline.
        diversification_deadline = min(
            budget_clock() + 4,
            search_budget.started_at + search_budget.total_seconds,
        )

        def diversification_should_stop():
            return (
                search_budget.reason() is not None
                or budget_clock() >= diversification_deadline
            )

        exploration = _copy_state(best_state)
        exploration_scoring = best_scoring
        focuses = _adaptive_violation_focuses(
            instances=instances, physicians=physicians, state=best_state,
            scoring=best_scoring,
            contract_by_physician=contract_by_physician,
            requests_by_physician_date=requests_by_physician_date,
        )
        focus = None
        if focuses:
            # Select among several expensive violations rather than repeatedly
            # rebuilding the same highest-scoring physician cohort.
            focus = rng.choice(focuses[:min(12, len(focuses))])
        schedule_start = min(instance.date for instance in instances)
        schedule_end = max(instance.date for instance in instances)
        window_days = rng.choice((14, 21, 28, 42))
        focus_dates = focus['dates'] if focus else []
        anchor = rng.choice(focus_dates) if focus_dates else rng.choice(instances).date
        core_start = max(
            schedule_start,
            anchor - timedelta(days=rng.randrange(0, max(window_days // 3, 1) + 1)),
        )
        core_end = min(
            schedule_end,
            core_start + timedelta(days=window_days - 1),
        )
        boundary_padding = _constraint_boundary_padding_days(
            contract_by_physician,
        )
        focus_start = max(
            schedule_start, core_start - timedelta(days=boundary_padding),
        )
        focus_end = min(
            schedule_end, core_end + timedelta(days=boundary_padding),
        )
        # Later restarts are cooler: they may still leave the basin, but cannot
        # discard an increasingly large fraction of the best score.
        temperature_ratio = max(
            Decimal('0.04'),
            Decimal('0.16') - Decimal(debug.get('restarts', 0)) * Decimal('0.04'),
        )
        maximum_score_increase = max(
            Decimal('5000'), best_scoring['score'] * temperature_ratio,
        )
        cohort_size = rng.choice((8, 10, 12))
        solver_state, solver_scoring, solver_debug = (
            _solve_bounded_multi_physician_neighborhood(
                instances=instances, physicians=physicians,
                state=exploration, scoring=exploration_scoring,
                manual_pairs=manual_pairs, targets=targets,
                contract_by_physician=contract_by_physician,
                requests_by_physician_date=requests_by_physician_date,
                eligible_facilities_by_physician=eligible_facilities_by_physician,
                minimum_rest_by_physician=minimum_rest_by_physician,
                rng=rng,
                focus_physician_ids=(focus or {}).get('physician_ids'),
                focus_start=focus_start, focus_end=focus_end,
                cohort_size=cohort_size,
                allow_non_improving=True,
                maximum_score_increase=maximum_score_increase,
                diversification_bias=True,
                should_stop=diversification_should_stop,
                time_limit_seconds=min(
                    4,
                    max(
                        diversification_deadline - budget_clock(),
                        0.1,
                    ),
                    max(
                        search_budget.total_seconds
                        - (search_budget.clock() - search_budget.started_at),
                        0.1,
                    ),
                ),
            )
        )
        if solver_debug.get('accepted'):
            exploration, exploration_scoring = solver_state, solver_scoring
            keep_progress(exploration, exploration_scoring)
        focus_physician_ids = set((focus or {}).get('physician_ids', ()))
        pairs = [
            pair for pair in _optimizer_pairs(exploration, manual_pairs)
            if (
                focus_start <= instances_by_id[pair[0]].date <= focus_end
                or pair[1] in focus_physician_ids
            )
        ]
        if len(pairs) < 2:
            return exploration, {
                'perturbed': _state_assignment_distance(best_state, exploration) > 0,
                'attempts': 0,
                'constraint_solver': solver_debug,
                'violation_focus': (focus or {}).get('violation_type'),
                'focus_physician_ids': sorted(focus_physician_ids),
                'focus_start': focus_start.isoformat(),
                'focus_end': focus_end.isoformat(),
                'core_focus_start': core_start.isoformat(),
                'core_focus_end': core_end.isoformat(),
                'boundary_padding_days': boundary_padding,
                'cohort_size': cohort_size,
                'maximum_score_increase': float(maximum_score_increase),
                'score_after_kick': float(exploration_scoring['score']),
                'distance_from_best': _state_assignment_distance(
                    best_state, exploration,
                ),
            }
        target_kicks = min(8, max(3, len(pairs) // 250))
        total_attempts = 0
        applied = []
        for kick_index in range(target_kicks):
            if diversification_should_stop():
                break
            pairs = _optimizer_pairs(exploration, manual_pairs)
            candidates = []
            attempt_limit = min(80, len(pairs) * 2)
            for _ in range(attempt_limit):
                if diversification_should_stop():
                    break
                total_attempts += 1
                left, right = rng.sample(pairs, 2)
                result = evaluate_plateau_pairwise_swap(
                    instances=instances, physicians=physicians, state=exploration,
                    instances_by_id=instances_by_id, manual_pairs=manual_pairs,
                    locked_open_instance_ids=locked_open_instance_ids,
                    targets=targets, contract_by_physician=contract_by_physician,
                    requests_by_physician_date=requests_by_physician_date,
                    eligible_facilities_by_physician=eligible_facilities_by_physician,
                    minimum_rest_by_physician=minimum_rest_by_physician,
                    current_score=exploration_scoring['score'],
                    left_instance_id=left[0], left_physician_id=left[1],
                    right_instance_id=right[0], right_physician_id=right[1],
                )
                if result.get('legal'):
                    projected_score = (
                        result.get('scoring') or {}
                    ).get('score', exploration_scoring['score'] + result.get('score_delta', 0))
                    if projected_score <= best_scoring['score'] + maximum_score_increase:
                        candidates.append((result.get('score_delta', Decimal('0')), left, right, result))
            if not candidates:
                break
            # Prefer a modest disruption, but deliberately continue after a
            # direct improvement so the restart actually enters a new basin.
            non_improving = [candidate for candidate in candidates if candidate[0] >= 0]
            candidate_pool = non_improving or candidates
            _delta, left, right, selected = min(candidate_pool, key=lambda row: row[0])
            exploration = selected['state']
            exploration_scoring = selected.get('scoring') or _score_schedule(
                instances, physicians, exploration, targets, contract_by_physician,
                requests_by_physician_date, eligible_facilities_by_physician,
                minimum_rest_by_physician,
            )
            keep_progress(exploration, exploration_scoring)
            applied.append({
                'kick': kick_index + 1,
                'left_shift_instance_id': left[0],
                'right_shift_instance_id': right[0],
                'score_after': float(exploration_scoring['score']),
            })

        return exploration, {
            'perturbed': bool(applied),
            'attempts': total_attempts,
            'constraint_solver': solver_debug,
            'violation_focus': (focus or {}).get('violation_type'),
            'focus_physician_ids': sorted(focus_physician_ids),
            'focus_start': focus_start.isoformat(),
            'focus_end': focus_end.isoformat(),
            'core_focus_start': core_start.isoformat(),
            'core_focus_end': core_end.isoformat(),
            'boundary_padding_days': boundary_padding,
            'cohort_size': cohort_size,
            'maximum_score_increase': float(maximum_score_increase),
            'kicks_applied': len(applied),
            'kick_details': applied,
            'score_after_kick': float(exploration_scoring['score']),
            'distance_from_best': _state_assignment_distance(
                best_state, exploration,
            ),
        }

    active_repairs = list(repairs)

    def launch_diversification(trigger):
        nonlocal rng, active_repairs, exploration_state, last_diversification_at
        diversification_started = monotonic()
        restart_seed = rng.getrandbits(63)
        rng = random.Random(restart_seed)
        active_repairs = list(repairs)
        exploration_state, restart_detail = diversify_from_best()
        debug['restarts'] += 1
        if trigger == 'proactive':
            debug['proactive_restarts'] += 1
        # A reconstructed state changes the available moves, so previously
        # unproductive strategies deserve one new trial in the new basin.
        # The exception is a repair that could not produce even one candidate:
        # preserve that stronger cooldown and retry it periodically instead of
        # paying its setup cost after every diversification.
        for stats in repair_stats.values():
            if int(stats.get('consecutive_empty_calls', 0)):
                continue
            stats['cooldown_until_cycle'] = debug['cycles']
            # A new basin merits another trial, but it does not erase lifetime
            # evidence that a strategy has repeatedly failed to improve the
            # retained global best. Preserving this count prevents frequent
            # proactive restarts from restoring every strategy to a full
            # four-second slice while the cleared cooldown still permits the
            # strategy to prove that the new basin made it productive.
        debug['restart_details'].append({
            'restart': debug['restarts'],
            'trigger': trigger,
            'seed': restart_seed,
            'runtime_seconds': monotonic() - diversification_started,
            **restart_detail,
        })
        last_diversification_at = budget_clock()

    def continue_from_new_best(previous_score, trigger_repair):
        """Start a fresh seeded search from the newly improved global best."""
        nonlocal rng, active_repairs, exploration_state, last_diversification_at
        continuation_seed = rng.getrandbits(63)
        rng = random.Random(continuation_seed)
        exploration_state = _copy_state(best_state)
        active_repairs = list(repairs)
        # This is a continuation from a slightly better version of the same
        # basin, not a reconstructed schedule.  Preserve each repair's
        # productivity and cooldown evidence.  Clearing it here caused every
        # small gain to re-enable all of the expensive zero-yield repairs,
        # repeatedly spending four-second slices on strategies that had
        # already demonstrated that they could not help this neighborhood.
        # A true diversification restart below still reopens every strategy
        # once because it materially changes the available moves.
        debug['best_chain_restarts'] += 1
        debug['best_chain_details'].append({
            'chain': debug['best_chain_restarts'],
            'trigger_repair': trigger_repair,
            'seed': continuation_seed,
            'score_before': float(previous_score),
            'score_after': float(best_scoring['score']),
        })
        last_diversification_at = budget_clock()

    def finish_pipeline_epoch(trigger):
        nonlocal pipeline_epoch_open
        if not pipeline_epoch_open:
            return None
        detail = {
            'epoch': pipeline_epoch_number,
            'seed': pipeline_epoch_seed,
            'restart_mode': pipeline_epoch_kind,
            'trigger': trigger,
            'starting_score': float(pipeline_epoch_start_score),
            'ending_score': float(best_scoring['score']),
            'score_improvement': float(
                max(pipeline_epoch_start_score - best_scoring['score'], Decimal('0'))
            ),
            'improvements': (
                int(debug.get('improvements', 0))
                - pipeline_epoch_start_improvements
            ),
            'runtime_seconds': budget_clock() - pipeline_epoch_started_at,
        }
        detail['productive'] = bool(
            detail['score_improvement'] > 0 or detail['improvements'] > 0
        )
        debug['pipeline_epochs'].append(detail)
        pipeline_epoch_open = False
        return detail

    def renew_pipeline_epoch(trigger):
        """Reopen the complete repair portfolio from the global best."""
        nonlocal rng, active_repairs, exploration_state, last_diversification_at
        nonlocal pipeline_epoch_number, pipeline_epoch_started_at
        nonlocal pipeline_epoch_start_score, pipeline_epoch_start_improvements
        nonlocal pipeline_epoch_seed, pipeline_epoch_open
        nonlocal pipeline_epoch_kind
        nonlocal consecutive_exhausted_pipeline_epochs
        finished_epoch = finish_pipeline_epoch(trigger)
        consecutive_exhausted_pipeline_epochs, transition = (
            _pipeline_epoch_transition(
                consecutive_exhausted_pipeline_epochs,
                productive=bool(finished_epoch and finished_epoch['productive']),
                epoch_kind=pipeline_epoch_kind,
            )
        )
        debug['consecutive_exhausted_pipeline_epochs'] = (
            consecutive_exhausted_pipeline_epochs
        )
        if transition == 'stop':
            # A deep epoch has already reset all seed-local portfolio evidence
            # and rerun the bounded portfolio from the retained global best.
            # If it is also empty, further identical retries are not a useful
            # way to consume the scheduler's remaining runtime ceiling.
            debug['productivity_exhausted'] = True
            debug['restart_exhausted'] = True
            return False
        if (
            finished_epoch
            and not finished_epoch['productive']
            and not any(
                repair_name == 'weekend_support_cycles'
                for repair_name, _repair, _kwargs in repairs
            )
        ):
            # Escalate only after the ordinary neighborhoods complete a full
            # zero-gain epoch. Three-person cycles are more expensive, but can
            # cross a pairwise legality barrier while preserving each person's
            # assignment count.
            repairs.append((
                'weekend_support_cycles',
                _repair_weekend_support_cycles,
                {'candidate_limit': 250},
            ))
            debug['coupled_weekend_repair_activations'] += 1
        deep_restart = transition == 'deep_restart'
        pipeline_epoch_seed = rng.getrandbits(63)
        rng = random.Random(pipeline_epoch_seed)
        pipeline_epoch_number += 1
        pipeline_epoch_started_at = budget_clock()
        pipeline_epoch_start_score = best_scoring['score']
        pipeline_epoch_start_improvements = int(debug.get('improvements', 0))
        pipeline_epoch_kind = 'deep' if deep_restart else 'soft'
        pipeline_epoch_open = True
        exploration_state = _copy_state(best_state)
        active_repairs = list(repairs)
        # Reopen each strategy once for the new basin while preserving its
        # lifetime productivity evidence. That evidence prevents a fresh seed
        # from repeatedly spending most of its budget on proven low-yield
        # repairs, but the cleared cooldown still lets a strategy discover
        # newly available moves.
        if deep_restart:
            _reset_adaptive_repair_epoch_state(repair_stats, debug['cycles'])
            debug['deep_pipeline_epoch_restarts'] += 1
            debug['deep_pipeline_epoch_details'].append({
                'epoch': pipeline_epoch_number,
                'seed': pipeline_epoch_seed,
                'trigger': 'two_consecutive_zero_gain_epochs',
                'starting_score': float(best_scoring['score']),
            })
        else:
            for stats in repair_stats.values():
                stats['cooldown_until_cycle'] = debug['cycles']
        search_budget.restart_count = 0
        search_budget.consecutive_unproductive_restarts = 0
        search_budget.improved_since_restart = False
        search_budget.restart_exhausted = False
        search_budget.last_improvement = budget_clock()
        debug['pipeline_epoch_restarts'] += 1
        debug['seed_epochs'] += 1
        last_diversification_at = budget_clock()
        return True

    while True:
        stop_reason = search_budget.reason()
        if stop_reason in (
            'overall_runtime_limit', 'user_stop', 'score_zero',
            'proven_score_floor',
        ):
            break
        if stop_reason == 'stall_limit':
            if not search_budget.restart_after_stall():
                if search_budget.restart_exhausted:
                    # A batch of unproductive seeds does not prove that the
                    # current best is globally exhausted. Roll into another
                    # independently seeded batch while preserving the absolute
                    # runtime cap. This is the in-run equivalent of the manual
                    # optimizer restarts that previously found later gains.
                    elapsed = budget_clock() - search_budget.started_at
                    remaining = search_budget.total_seconds - elapsed
                    if remaining > 1:
                        renewed = renew_pipeline_epoch('stalled_search_batch')
                        cycles_without_global_improvement = 0
                        if renewed:
                            continue
                        break
                    debug['restart_exhausted'] = True
                    debug['consecutive_unproductive_restarts'] = (
                        search_budget.consecutive_unproductive_restarts
                    )
                break
            launch_diversification('stall')
            cycles_without_global_improvement = 0
            continue
        if stop_reason is not None:
            break
        if debug['cycles'] and (
            cycles_without_global_improvement >= 2
            or budget_clock() - last_diversification_at
            >= proactive_restart_interval_seconds
        ):
            launch_diversification('proactive')
            cycles_without_global_improvement = 0
            if search_budget.reason() is not None:
                continue
        debug['cycles'] += 1
        cycle_number = debug['cycles']
        cycle_score_before = best_scoring['score']
        cycle_generation_before = best_generation
        cycle_instances = list(instances)
        rng.shuffle(cycle_instances)
        scheduled_repairs = _adaptive_repair_order(
            active_repairs, repair_stats, cycle_number,
        )
        # If every repair is cooling down, diversify immediately rather than
        # spin through empty cycles until a cooldown expires.
        if not scheduled_repairs:
            launch_diversification('proactive')
            cycles_without_global_improvement = 0
            continue
        for repair_name, repair, options in scheduled_repairs:
            if search_budget.reason() is not None:
                break
            repair_started = monotonic()
            prior_stats = repair_stats.get(repair_name) or {}
            allocated_slice_seconds = _adaptive_repair_slice_seconds(prior_stats)
            slice_end = repair_started + allocated_slice_seconds
            score_before_repair = best_scoring['score']
            repair_kwargs = {
                'instances': cycle_instances,
                'physicians': physicians,
                'state': exploration_state,
                'manual_pairs': manual_pairs,
                'targets': targets,
                'contract_by_physician': contract_by_physician,
                'requests_by_physician_date': requests_by_physician_date,
                'eligible_facilities_by_physician': eligible_facilities_by_physician,
                'minimum_rest_by_physician': minimum_rest_by_physician,
                'should_stop': lambda: (
                    search_budget.reason() is not None or monotonic() >= slice_end
                ),
                'candidate_limit': 600,
                'on_improvement': keep_progress,
            }
            # Strategy-specific options intentionally override controller
            # defaults. Building one mapping avoids duplicate keyword errors
            # when a bounded escalation selects a smaller candidate budget.
            repair_kwargs.update(options)
            exploration_state, _, repair_debug = repair(**repair_kwargs)
            debug['attempts'] += repair_debug['attempts']
            stats = repair_stats.setdefault(repair_name, {
                'calls': 0, 'attempts': 0, 'legal_candidates': 0,
                'scored_candidates': 0, 'accepts': 0, 'runtime_seconds': 0,
                'epoch_calls': 0,
            })
            stats['calls'] += 1
            stats['epoch_calls'] = int(stats.get('epoch_calls', 0)) + 1
            stats['allocated_slice_seconds'] = (
                float(stats.get('allocated_slice_seconds', 0) or 0)
                + allocated_slice_seconds
            )
            stats['last_allocated_slice_seconds'] = allocated_slice_seconds
            stats['attempts'] += repair_debug['attempts']
            stats['legal_candidates'] += repair_debug.get('legal_candidates', 0)
            stats['scored_candidates'] += repair_debug.get('scored_candidates', 0)
            stats['accepts'] += len(repair_debug['accepts'])
            repair_runtime = monotonic() - repair_started
            _record_adaptive_repair_productivity(
                stats,
                score_gain=max(score_before_repair - best_scoring['score'], Decimal('0')),
                runtime_seconds=repair_runtime,
                cycle=cycle_number,
                attempts=repair_debug['attempts'],
            )
            if best_generation > cycle_generation_before:
                continue_from_new_best(score_before_repair, repair_name)
                break
        if best_scoring['score'] < cycle_score_before:
            cycles_without_global_improvement = 0
        else:
            cycles_without_global_improvement += 1
    finish_pipeline_epoch(
        'productivity_exhausted'
        if debug.get('productivity_exhausted')
        else search_budget.reason() or 'search_complete'
    )
    return best_state, best_scoring, debug


def _night_fix_sources(instances_by_id, physicians, state, manual_pairs, contract_by_physician):
    instances = list(instances_by_id.values())
    report = _night_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
    )
    violating_physician_ids = {
        violation['physician_id']
        for violation in report['night_violations']
    }
    night_counts = {
        item['physician_id']: item['night_shifts']
        for item in report['night_shifts_by_physician']
    }
    total_nights = report['total_night_shifts']
    average_nights = (
        Decimal(total_nights) / Decimal(len(physicians))
        if physicians
        else Decimal('0')
    )
    high_night_physician_ids = {
        physician_id
        for physician_id, count in night_counts.items()
        if Decimal(count) > average_nights + Decimal('1')
    }
    isolated_night_physician_ids = set()
    night_instances_by_physician = defaultdict(list)
    for instance_id, physician_ids in state.items():
        instance = instances_by_id.get(instance_id)
        if instance is None or not instance.shift_template.night_shift:
            continue
        for physician_id in physician_ids:
            night_instances_by_physician[physician_id].append(instance)
    for physician_id, night_instances in night_instances_by_physician.items():
        if any(len(block) == 1 for block in _night_blocks(night_instances)):
            isolated_night_physician_ids.add(physician_id)

    source_physician_ids = (
        violating_physician_ids
        .union(high_night_physician_ids)
        .union(isolated_night_physician_ids)
    )
    sources = []
    for instance_id, physician_ids in state.items():
        instance = instances_by_id.get(instance_id)
        if instance is None or not instance.shift_template.night_shift:
            continue
        for physician_id in physician_ids:
            if physician_id not in source_physician_ids:
                continue
            if (instance_id, physician_id) in manual_pairs:
                continue
            sources.append((night_counts.get(physician_id, 0), physician_id, instance_id))
    return [
        (physician_id, instance_id)
        for _night_count, physician_id, instance_id in sorted(sources, reverse=True)
    ], night_counts


def _violation_explanation(violation_type):
    explanations = {
        'SAME_SHIFT_STREAK': 'Physician is assigned to too many consecutive occurrences of the same shift template.',
        'NIGHT_UNDER_MINIMUM': 'Physician is below the configured minimum night-shift count for the period.',
        'NIGHT_OVER_MAXIMUM': 'Physician is above the configured maximum night-shift count for the period.',
        'MIN_CONSECUTIVE_NIGHTS': 'Physician is assigned to fewer consecutive night shifts than configured.',
        'MAX_CONSECUTIVE_NIGHTS': 'Physician is assigned to more consecutive night shifts than configured.',
        'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT': 'Physician returned to a non-night shift too soon after a night block.',
        'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK': 'Physician started another night block too soon after the prior night block.',
        'NIGHT_CONCENTRATION': 'Physician has a higher night-shift load than the v0 default distribution target.',
        'REQUEST_DAY_OFF_VIOLATION': 'Assigned shift conflicts with a Day Off request.',
        'REQUEST_SHIFT_OFF_VIOLATION': 'Assigned shift conflicts with a Shift Off request.',
        'REQUEST_DAY_ON_REWARD': 'Assigned shift satisfied a Day On request and reduced request score.',
        'REQUEST_SHIFT_ON_REWARD': 'Assigned shift satisfied a Shift On request and reduced request score.',
        'REQUEST_DAY_ON_UNMET': 'No assignment satisfied this Day On request.',
        'REQUEST_SHIFT_ON_UNMET': 'No assignment satisfied this Shift On request.',
    }
    return explanations.get(violation_type, 'Optimizer v0 scoring assigned this penalty.')


def _report_violation_row(violation, violation_type=None):
    row_type = violation_type or violation.get('violation_type')
    penalty_amount = violation.get('penalty_amount', violation.get('penalty', 0))
    return {
        'violation_type': row_type,
        'dates_involved': violation.get('dates_involved', []),
        'night_block_dates': violation.get('night_block_dates', []),
        'prior_night_block_dates': violation.get('prior_night_block_dates', []),
        'next_night_block_dates': violation.get('next_night_block_dates', []),
        'night_block_assignments': violation.get('night_block_assignments', []),
        'prior_night_block_assignments': violation.get('prior_night_block_assignments', []),
        'next_night_block_assignments': violation.get('next_night_block_assignments', []),
        'assignment_details': violation.get('assignment_details', []),
        'shift_instance_ids': violation.get('shift_instance_ids', []),
        'facility': violation.get('facility'),
        'shift_template': violation.get('shift_template'),
        'previous_assignment': violation.get('previous_assignment'),
        'next_assignment': violation.get('next_assignment'),
        'period_type': violation.get('period_type'),
        'period_start': violation.get('period_start'),
        'period_end': violation.get('period_end'),
        'contract_id': violation.get('contract_id'),
        'contract_name': violation.get('contract_name'),
        'request_id': violation.get('request_id'),
        'request_type': violation.get('request_type'),
        'request_scope': violation.get('request_scope'),
        'request_weight': violation.get('request_weight'),
        'configured_limit': violation.get('configured_limit'),
        'actual_value': violation.get('actual_value'),
        'penalty_weight': violation.get('penalty_weight'),
        'penalty_amount': penalty_amount,
        'explanation': (violation.get('explanation') or _violation_explanation(row_type))
        if (row_type or '').startswith('WEEKEND') else _violation_explanation(row_type),
    }


def _report_sort_key(row):
    first_date = row['dates_involved'][0] if row['dates_involved'] else ''
    return (-float(row['penalty_amount'] or 0), row['violation_type'] or '', first_date)


def _validated_night_report_for_current_assignments(
    night_report, version, assignments, boundary_context=None,
):
    """Drop stale rows while accepting verified immutable boundary references."""
    boundary_context = boundary_context or {}
    assigned_pairs = {
        (assignment.shift_instance_id, assignment.physician_id)
        for assignment in assignments
    }
    instances_by_id = {
        assignment.shift_instance_id: assignment.shift_instance
        for assignment in assignments
    }
    boundary_pairs = {
        (instance.id, physician_id)
        for physician_id, prior_instances in boundary_context.items()
        for instance in prior_instances
    }
    boundary_instances_by_id = {
        instance.id: instance
        for prior_instances in boundary_context.values()
        for instance in prior_instances
    }
    all_instances_by_id = {**boundary_instances_by_id, **instances_by_id}
    filtered_violations = []
    validation_errors = []
    dropped_count = 0

    def add_error(violation, message):
        return {
            'violation_type': violation.get('violation_type'),
            'physician_id': violation.get('physician_id'),
            'shift_instance_ids': violation.get('shift_instance_ids', []),
            'message': message,
        }

    for violation in night_report['night_violations']:
        physician_id = violation['physician_id']
        valid = True
        for instance_id in violation.get('shift_instance_ids', []):
            instance = all_instances_by_id.get(instance_id)
            if instance is None:
                validation_errors.append(add_error(violation, f'Shift instance {instance_id} is not currently assigned.'))
                valid = False
                break
            is_current_pair = (instance_id, physician_id) in assigned_pairs
            is_boundary_pair = (instance_id, physician_id) in boundary_pairs
            if not (is_current_pair or is_boundary_pair):
                validation_errors.append(add_error(violation, f'Shift instance {instance_id} is not assigned to this physician.'))
                valid = False
                break
            if is_current_pair and instance.schedule_version_id != version.id:
                validation_errors.append(add_error(violation, f'Shift instance {instance_id} belongs to a different Schedule Version.'))
                valid = False
                break
            if is_current_pair and not (
                version.schedule_block.start_date
                <= instance.date
                <= version.schedule_block.end_date
            ):
                validation_errors.append(add_error(violation, f'Shift instance {instance_id} is outside the Schedule Block date range.'))
                valid = False
                break

        if not valid:
            dropped_count += 1
            continue

        night_block_ids = {
            item.get('shift_instance_id')
            for item in violation.get('night_block_assignments', [])
        }
        for instance_id in night_block_ids:
            instance = all_instances_by_id.get(instance_id)
            if instance is None or not instance.shift_template.night_shift:
                validation_errors.append(add_error(violation, f'Night block shift instance {instance_id} is not night-designated.'))
                valid = False
                break

        previous_assignment = violation.get('previous_assignment')
        if valid and previous_assignment:
            instance = all_instances_by_id.get(previous_assignment.get('shift_instance_id'))
            if instance is None or instance.shift_template.night_shift:
                validation_errors.append(add_error(violation, 'Previous assignment boundary is missing or night-designated.'))
                valid = False

        next_assignment = violation.get('next_assignment')
        if valid and next_assignment:
            instance = all_instances_by_id.get(next_assignment.get('shift_instance_id'))
            if instance is None or instance.shift_template.night_shift:
                validation_errors.append(add_error(violation, 'Next assignment boundary is missing or night-designated.'))
                valid = False

        if valid:
            filtered_violations.append(violation)
        else:
            dropped_count += 1

    filtered_report = {
        **night_report,
        'night_violations': filtered_violations,
        'night_violations_count': len(filtered_violations),
        'stale_violation_rows_dropped': dropped_count,
        'violation_assignment_validation_errors': validation_errors,
    }
    return filtered_report


def _score_audit(scoring, night_report, request_rows):
    warnings = []
    breakdown = scoring['breakdown']
    component_total = sum(
        value
        for key, value in breakdown.items()
        if key != 'total_score'
    )
    if abs(component_total - breakdown['total_score']) > Decimal('0.0001'):
        warnings.append('Score/report mismatch detected: total_score does not equal score breakdown sum.')

    night_rows_total = sum(
        Decimal(str(violation.get('penalty', 0)))
        for violation in night_report['night_violations']
    )
    if abs(night_rows_total - breakdown['night_score']) > Decimal('0.0001'):
        warnings.append('Score/report mismatch detected: night_score does not equal listed night penalty rows.')

    request_rows_total = sum(
        Decimal(str(row.get('penalty', 0)))
        for row in request_rows
    )
    if abs(request_rows_total - breakdown['request_score']) > Decimal('0.0001'):
        warnings.append('Score/report mismatch detected: request_score does not equal listed request scoring rows.')

    workload_rows_total = sum(
        Decimal(str(row.get('score_contribution_exact', row.get('score_contribution', 0))))
        for row in scoring.get('workload_score_rows', [])
    )
    if abs(workload_rows_total - breakdown['workload_score']) > Decimal('0.0001'):
        warnings.append('Score/report mismatch detected: workload_score does not equal listed workload scoring rows.')

    return {
        'warnings': warnings,
        'component_total': float(component_total),
        'total_score': float(breakdown['total_score']),
        'night_rows_total': float(night_rows_total),
        'night_score': float(breakdown['night_score']),
        'request_rows_total': float(request_rows_total),
        'request_score': float(breakdown['request_score']),
        'workload_rows_total': float(workload_rows_total),
        'workload_score': float(breakdown['workload_score']),
    }


def build_violation_report(schedule_version, optimizer_run=None):
    version = (
        ScheduleVersion.objects
        .select_related('schedule_block', 'domain')
        .get(id=schedule_version.id)
    )
    instances = list(
        _version_shift_instances_queryset(version)
        .select_related('facility', 'shift_template')
        .order_by('date', 'facility__name', 'start_datetime', 'id')
    )
    if optimizer_run is None:
        optimizer_run = version.optimizer_runs.filter(is_active=True).order_by('-run_number').first()
    assignments = list(
        assignments_for_viewed_run(version, optimizer_run)
        .select_related('shift_instance__facility', 'shift_instance__shift_template', 'physician__user')
    )
    active_contract_assignments = list(
        ContractUserAssignment.objects.filter(
            domain=version.domain,
            contract__active=True,
            physician__active=True,
        )
        .select_related('physician__user', 'contract')
        .prefetch_related('contract__facilities')
        .order_by('physician__display_name', 'physician__user__last_name', 'physician__user__first_name', 'physician_id')
    )
    physicians = [assignment.physician for assignment in active_contract_assignments]
    contract_by_physician = {
        assignment.physician_id: assignment.contract
        for assignment in active_contract_assignments
    }
    manual_assignment_only_physician_ids = {
        physician_id
        for physician_id, contract in contract_by_physician.items()
        if contract.manual_assignment_only
    }
    scoring_physicians = [
        physician for physician in physicians
        if physician.id not in manual_assignment_only_physician_ids
    ]
    scoring_contract_by_physician = {
        physician_id: contract
        for physician_id, contract in contract_by_physician.items()
        if physician_id not in manual_assignment_only_physician_ids
    }
    _attach_published_boundary_context(
        version,
        instances,
        scoring_contract_by_physician,
    )
    minimum_rest_by_physician = {
        assignment.physician_id: _minimum_rest_hours(assignment.contract)
        for assignment in active_contract_assignments
    }
    eligible_facilities_by_physician = {
        assignment.physician_id: {
            facility.id for facility in assignment.contract.facilities.all()
        }
        for assignment in active_contract_assignments
    }
    state, _manual_pairs = _state_from_assignments(assignments)

    total_required_hours = sum(
        _shift_hours(instance) * instance.required_staffing
        for instance in instances
    )
    total_required_slots = sum(instance.required_staffing for instance in instances)
    default_hours_target = (
        total_required_hours / Decimal(len(physicians))
        if physicians
        else Decimal('0')
    )
    default_shift_target = (
        Decimal(total_required_slots) / Decimal(len(physicians))
        if physicians
        else Decimal('0')
    )
    targets = {
        physician.id: _version_contract_target(
            version, physician.id, contract_by_physician[physician.id],
            default_hours_target,
            default_shift_target,
        )
        for physician in physicians
    }
    for physician_id in manual_assignment_only_physician_ids:
        targets[physician_id] = {
            'units': 'HOURS', 'target': Decimal('0'), 'minimum': Decimal('0'),
            'maximum': Decimal('0'), 'rules': [],
        }
    requests_by_physician_date = defaultdict(list)
    for schedule_request in (
        ScheduleRequest.objects.filter(
            schedule_block=version.schedule_block,
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .prefetch_related('shift_templates')
    ):
        requests_by_physician_date[
            (schedule_request.physician_id, schedule_request.date)
        ].append(schedule_request)

    fixed_request_diagnostic = _fixed_shift_on_request_workload_floor(
        instances,
        physicians,
        targets,
        requests_by_physician_date,
        eligible_facilities_by_physician,
        state=state,
        manual_assignment_only_physician_ids=manual_assignment_only_physician_ids,
    )

    scoring = _score_schedule(
        instances,
        physicians,
        state,
        targets,
        contract_by_physician,
        requests_by_physician_date,
        eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    night_report = _night_violation_report(
        instances,
        scoring_physicians,
        state,
        scoring_contract_by_physician,
    )
    night_report = _validated_night_report_for_current_assignments(
        night_report,
        version,
        assignments,
        boundary_context=_published_boundary_context(instances),
    )
    request_rows = _request_scoring_rows(
        instances,
        scoring_physicians,
        state,
        scoring_contract_by_physician,
        requests_by_physician_date,
    )
    score_audit = _score_audit(scoring, night_report, request_rows)

    users = {
        physician.id: {
            'user_id': physician.id,
            'display_name': _physician_display_name(physician),
            'total_score': 0,
            'shifts': scoring['physician_shifts'][physician.id],
            'hours': float(scoring['physician_hours'][physician.id]),
            'night_shifts': scoring['physician_night_shifts'][physician.id],
            'violations': [],
            'workload_score': None,
        }
        for physician in physicians
    }

    for row in scoring['workload_score_rows']:
        physician_id = row['physician_id']
        if physician_id not in users:
            continue
        floor_rows = [
            floor_row
            for floor_row in fixed_request_diagnostic['floor_rows']
            if floor_row['physician_id'] == physician_id
        ]
        users[physician_id]['workload_score'] = {
            **row,
            'fixed_request_floor_rows': floor_rows,
            'fixed_request_assessment_rows': [
                assessment
                for assessment in fixed_request_diagnostic['assessment_rows']
                if assessment['physician_id'] == physician_id
            ],
            'unavoidable_due_to_fixed_requests': bool(floor_rows),
        }
        users[physician_id]['total_score'] += row['score_contribution']

    for violation in scoring['same_shift_violations']:
        physician_id = violation['physician_id']
        if physician_id not in users:
            continue
        row = _report_violation_row(violation, violation_type='SAME_SHIFT_STREAK')
        users[physician_id]['violations'].append(row)
        users[physician_id]['total_score'] += row['penalty_amount'] or 0

    for violation in night_report['night_violations']:
        physician_id = violation['physician_id']
        if physician_id not in users:
            continue
        row = _report_violation_row(violation)
        users[physician_id]['violations'].append(row)
        users[physician_id]['total_score'] += row['penalty_amount'] or 0

    for violation in request_rows:
        physician_id = violation['physician_id']
        if physician_id not in users:
            continue
        row = _report_violation_row(violation)
        users[physician_id]['violations'].append(row)
        users[physician_id]['total_score'] += row['penalty_amount'] or 0

    weekend_report = _weekend_volume_report(
        instances, scoring_physicians, state, scoring_contract_by_physician, details=True,
    )
    for violation in weekend_report['violations']:
        row = _report_violation_row(violation)
        users[violation['physician_id']]['violations'].append(row)
        users[violation['physician_id']]['total_score'] += row['penalty_amount'] or 0
    weekend_rows_total = sum((Decimal(str(row['penalty'])) for row in weekend_report['violations']), Decimal('0'))
    score_audit['weekend_rows_total'] = float(weekend_rows_total)
    score_audit['weekend_score'] = float(scoring['breakdown']['weekend_score'])
    if abs(weekend_rows_total - scoring['breakdown']['weekend_score']) > Decimal('0.0001'):
        score_audit['warnings'].append('Score/report mismatch detected: weekend_score does not equal listed weekend penalty rows.')

    shift_rule_report = _shift_period_report(
        instances, scoring_physicians, state, scoring_contract_by_physician, details=True,
    )
    for violation in shift_rule_report['violations']:
        row = _report_violation_row(violation)
        users[violation['physician_id']]['violations'].append(row)
        users[violation['physician_id']]['total_score'] += row['penalty_amount'] or 0
    shift_rule_rows_total = sum(
        (Decimal(str(row['penalty'])) for row in shift_rule_report['violations']),
        Decimal('0'),
    )
    score_audit['shift_rule_rows_total'] = float(shift_rule_rows_total)
    score_audit['shift_rule_score'] = float(scoring['breakdown']['shift_rule_score'])
    if abs(shift_rule_rows_total - scoring['breakdown']['shift_rule_score']) > Decimal('0.0001'):
        score_audit['warnings'].append(
            'Score/report mismatch detected: shift_rule_score does not equal listed shift-rule penalty rows.'
        )

    for user in users.values():
        user['violations'] = sorted(user['violations'], key=_report_sort_key)

    warnings = []
    if night_report['night_unresolved_reasons']:
        warnings.extend(night_report['night_unresolved_reasons'])
    if night_report['stale_violation_rows_dropped']:
        warnings.append('Dropped stale violation row.')
    warnings.extend(score_audit['warnings'])
    for row in fixed_request_diagnostic['floor_rows']:
        warnings.append(
            f"Unavoidable request-compliant workload penalty: {row['physician']} has "
            f"{row['fixed_request_required_value']:g} {row['units'].lower()} required by "
            f"Fixed Shift On requests for {row['period_start']} through {row['period_end']}, "
            f"above the configured maximum of {row['effective_max_value']:g}."
        )
    for row in fixed_request_diagnostic['assessment_rows']:
        if not row['potentially_avoidable_penalty']:
            continue
        warnings.append(
            f"Request check: Fixed Shift On requests require {row['fixed_request_required_value']:g} "
            f"{row['units'].lower()} for {row['physician']} during {row['period_start']} through "
            f"{row['period_end']}, within the configured maximum of {row['effective_max_value']:g}; "
            f"the viewed schedule assigns {row['actual_assigned_value']:g}. The resulting "
            f"{row['potentially_avoidable_penalty']:g}-point workload penalty is not forced by those requests."
        )
    if fixed_request_diagnostic['unresolved_request_count']:
        warnings.append(
            f"{fixed_request_diagnostic['unresolved_request_count']} Fixed Shift On request(s) "
            'could not be resolved to one schedule shift instance; they were excluded from the proven score floor.'
        )
    for violation in night_report['night_violations']:
        if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT':
            warnings.append(
                f"Configured {violation['configured_limit']} days off after night block before non-night work creates violations in this "
                f"{(version.schedule_block.end_date - version.schedule_block.start_date).days + 1}-day schedule block "
                f"with {night_report['total_night_shifts']} night shifts."
            )
            break

    return {
        'schedule_version': {
            'id': version.id,
            'schedule_block': version.schedule_block_id,
            'domain': version.domain_id,
            'domain_name': version.domain.name,
            'version_number': version.version_number,
            'name': version.name,
            'status': version.status,
        },
        'schedule_block': {
            'id': version.schedule_block_id,
            'name': version.schedule_block.generated_name,
            'start_date': version.schedule_block.start_date.isoformat(),
            'end_date': version.schedule_block.end_date.isoformat(),
        },
        'optimizer_run': (
            {
                'id': optimizer_run.id,
                'schedule_version': optimizer_run.schedule_version_id,
                'run_number': optimizer_run.run_number,
                'created_at': optimizer_run.created_at.isoformat(),
                'status': optimizer_run.status,
                'initial_score': float(optimizer_run.initial_score) if optimizer_run.initial_score is not None else None,
                'final_score': float(optimizer_run.final_score) if optimizer_run.final_score is not None else None,
                'is_active': optimizer_run.is_active,
                'score_is_stale': optimizer_run.score_is_stale,
            }
            if optimizer_run is not None
            else None
        ),
        'total_score': float(scoring['score']),
        'score_breakdown': {
            key: float(value)
            for key, value in scoring['breakdown'].items()
        },
        'warnings': list(dict.fromkeys(warnings)),
        'fixed_request_feasibility': fixed_request_diagnostic,
        'score_audit': score_audit,
        'debug': {
            'violations_recomputed_from_final_assignments': True,
            'stale_violation_rows_dropped': night_report['stale_violation_rows_dropped'],
            'violation_assignment_validation_errors': night_report['violation_assignment_validation_errors'],
            'contracts_used_by_physician': night_report['contracts_used_by_physician'],
            'night_rules_by_contract': night_report['night_rules_by_contract'],
            'physicians_without_contract': night_report['physicians_without_contract'],
            'night_block_candidates_by_physician': night_report['night_block_candidates_by_physician'],
            'night_blocks_assigned': _night_block_debug(
                instances,
                physicians,
                state,
                contract_by_physician,
            )['night_blocks_assigned'],
            'night_minimum_violations_by_contract': night_report['night_minimum_violations_by_contract'],
            'night_maximum_violations_by_contract': night_report['night_maximum_violations_by_contract'],
            'min_consecutive_night_violations': night_report['min_consecutive_night_violations'],
            'max_consecutive_night_violations': night_report['max_consecutive_night_violations'],
            'post_night_to_non_night_recovery_violations': night_report['post_night_to_non_night_recovery_violations'],
            'post_night_to_next_night_block_recovery_violations': night_report['post_night_to_next_night_block_recovery_violations'],
            'night_minimum_rules_applied': night_report['night_minimum_rules_applied'],
            'night_minimum_rules_suppressed_as_duplicates': night_report['night_minimum_rules_suppressed_as_duplicates'],
            'night_minimum_violations_count': sum(
                1
                for violation in night_report['night_violations']
                if violation['violation_type'] == 'NIGHT_UNDER_MINIMUM'
            ),
            'workload_score_total': float(scoring['breakdown']['workload_score']),
            'workload_score_rows': scoring['workload_score_rows'],
            'fixed_request_feasibility': fixed_request_diagnostic,
            'night_block_assignment_ids_by_physician': {
                str(item['physician_id']): [
                    block['shift_instance_ids']
                    for block in item['blocks']
                ]
                for item in _night_block_debug(
                    instances,
                    physicians,
                    state,
                    contract_by_physician,
                )['night_blocks_by_physician']
            },
        },
        'users': sorted(
            users.values(),
            key=lambda user: user['display_name'].lower(),
        ),
    }


def _has_hard_invalids(scoring):
    validation = scoring['validation']
    return any(
        validation.get(key, 0)
        for key in (
            'final_overlap_violations',
            'final_rest_violations',
            'final_duplicate_violations',
            'final_overstaffed_violations',
            'final_inactive_physician_violations',
            'final_facility_ineligible_violations',
        )
    )


def _should_preserve_timeout_result(
    *, timed_out, initial_score, final_score, final_scoring, unfilled_shift_count,
    coverage_improved_to_complete=False,
):
    """Keep an anytime result only when it is complete, valid, and better."""
    return bool(
        timed_out
        and unfilled_shift_count == 0
        and not _has_hard_invalids(final_scoring)
        and (final_score < initial_score or coverage_improved_to_complete)
    )


def _result_priority(scoring, unfilled_shift_count):
    """Rank hard validity, then fixed request-on fulfillment, then soft score."""
    complete_valid = unfilled_shift_count == 0 and not _has_hard_invalids(scoring)
    return (
        0 if complete_valid else 1,
        scoring.get('fixed_request_on_unmet', 0),
        scoring['score'],
    )


def _optimization_priority(scoring):
    """Lexicographic objective for complete candidate schedules."""
    return (scoring.get('fixed_request_on_unmet', 0), scoring['score'])


def _state_result_priority(instances, state, scoring):
    """Rank a concrete state without allowing a cheaper incomplete state to win."""
    return _result_priority(scoring, _unfilled_slot_count(instances, state))


def _repair_direct_coverage(
    *, instances, physicians, state, manual_pairs, targets, contract_by_physician,
    requests_by_physician_date, eligible_facilities_by_physician,
    minimum_rest_by_physician, should_stop, candidate_limit=600,
    on_improvement=None,
):
    """Greedily fill directly feasible open slots using the official score as a tie-breaker."""
    del manual_pairs  # Additions cannot alter or remove preserved assignments.
    instances_by_id = {instance.id: instance for instance in instances}
    scoring_args = (
        targets, contract_by_physician, requests_by_physician_date,
        eligible_facilities_by_physician, minimum_rest_by_physician,
    )
    scoring = _score_schedule(instances, physicians, state, *scoring_args)
    attempts = 0
    legal_candidates = 0
    scored_candidates = 0
    accepts = []

    while attempts < candidate_limit and not should_stop():
        accepted = False
        for instance in instances:
            if should_stop() or attempts >= candidate_limit:
                break
            if instance.is_locked_open or len(state[instance.id]) >= instance.required_staffing:
                continue

            candidates = []
            for physician in physicians:
                if should_stop() or attempts >= candidate_limit:
                    break
                if physician.id in state[instance.id]:
                    continue
                attempts += 1
                if not _can_assign_in_state(
                    state, instances_by_id, instance, physician.id,
                    eligible_facilities_by_physician, minimum_rest_by_physician,
                ):
                    continue
                legal_candidates += 1
                trial = _copy_state(state)
                _add_to_state(trial, instance.id, physician.id)
                verified = _score_schedule(
                    instances, physicians, trial, *scoring_args,
                )
                scored_candidates += 1
                if _has_hard_invalids(verified):
                    continue
                candidates.append((
                    _state_result_priority(instances, trial, verified),
                    physician.id,
                    trial,
                    verified,
                ))

            if not candidates:
                continue
            _priority, physician_id, state, scoring = min(
                candidates, key=lambda row: (row[0], row[1]),
            )
            accepts.append({
                'shift_instance_id': instance.id,
                'physician_id': physician_id,
                'unfilled_after': _unfilled_slot_count(instances, state),
                'score_after': float(scoring['score']),
            })
            if on_improvement is not None:
                on_improvement(state, scoring)
            accepted = True
            break

        if not accepted:
            break

    if should_stop():
        reason = 'time_budget'
    elif attempts >= candidate_limit:
        reason = 'candidate_budget'
    elif _unfilled_slot_count(instances, state) == 0:
        reason = 'coverage_complete'
    else:
        reason = 'no_directly_feasible_assignment'
    return state, scoring, {
        'attempts': attempts,
        'legal_candidates': legal_candidates,
        'scored_candidates': scored_candidates,
        'accepts': accepts,
        'stopped_reason': reason,
    }


def evaluate_plateau_pairwise_swap(
    *, instances, physicians, state, instances_by_id, manual_pairs,
    locked_open_instance_ids, targets, contract_by_physician,
    requests_by_physician_date, eligible_facilities_by_physician,
    minimum_rest_by_physician, current_score, left_instance_id,
    left_physician_id, right_instance_id, right_physician_id,
):
    """Evaluate one plateau swap with the official persisted score objective."""
    if left_instance_id == right_instance_id or left_physician_id == right_physician_id:
        return {'legal': False, 'reason': 'duplicate_or_noop_swap'}
    if left_instance_id in locked_open_instance_ids or right_instance_id in locked_open_instance_ids:
        return {'legal': False, 'reason': 'locked_open'}
    if (
        (left_instance_id, left_physician_id) in manual_pairs
        or (right_instance_id, right_physician_id) in manual_pairs
    ):
        return {'legal': False, 'reason': 'locked_assignment'}
    if (
        right_physician_id in state[left_instance_id]
        or left_physician_id in state[right_instance_id]
    ):
        return {'legal': False, 'reason': 'duplicate_assignment'}

    left_instance = instances_by_id[left_instance_id]
    right_instance = instances_by_id[right_instance_id]
    trial_state = _copy_state(state)
    _replace_in_state(
        trial_state, left_instance_id, left_physician_id, right_physician_id,
    )
    _replace_in_state(
        trial_state, right_instance_id, right_physician_id, left_physician_id,
    )
    for side, instance, physician_id, exclude_id in (
        ('left', left_instance, right_physician_id, left_instance_id),
        ('right', right_instance, left_physician_id, right_instance_id),
    ):
        if instance.facility_id not in eligible_facilities_by_physician.get(physician_id, set()):
            return {'legal': False, 'reason': 'not_facility_eligible', 'invalid_side': side}
        intervals = _intervals_for_physician(
            trial_state, instances_by_id, physician_id, exclude_instance_id=exclude_id,
        )
        if _overlaps(instance, intervals):
            return {'legal': False, 'reason': 'overlap', 'invalid_side': side}
        if _rest_violation(instance, intervals, minimum_rest_by_physician[physician_id]):
            return {'legal': False, 'reason': 'rest_violation', 'invalid_side': side}
    incremental_delta = _selected_physician_score_delta(
        instances,
        physicians,
        state,
        trial_state,
        {left_physician_id, right_physician_id},
        targets,
        contract_by_physician,
        requests_by_physician_date,
        eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    if incremental_delta >= 0:
        return {
            'legal': True,
            'improving': False,
            'score_delta': incremental_delta,
            'incremental_score_delta': incremental_delta,
            'incremental_rejection': True,
            'scoring': None,
            'state': trial_state,
        }
    trial_scoring = _score_schedule(
        instances, physicians, trial_state, targets, contract_by_physician,
        requests_by_physician_date, eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    delta = trial_scoring['score'] - current_score
    return {
        'legal': True,
        'improving': delta < 0,
        'score_delta': delta,
        'incremental_score_delta': incremental_delta,
        'incremental_rejection': False,
        'scoring': trial_scoring,
        'state': trial_state,
    }


def evaluate_plateau_three_way_rotation(
    *, instances, physicians, state, instances_by_id, manual_pairs,
    locked_open_instance_ids, targets, contract_by_physician,
    requests_by_physician_date, eligible_facilities_by_physician,
    minimum_rest_by_physician, current_score, assignment_pairs,
    new_physician_ids,
):
    """Evaluate a bounded three-person rotation with the official objective."""
    if len(assignment_pairs) != 3 or len(new_physician_ids) != 3:
        return {'legal': False, 'reason': 'rotation_requires_three_assignments'}
    instance_ids = [pair[0] for pair in assignment_pairs]
    old_physician_ids = [pair[1] for pair in assignment_pairs]
    if len(set(instance_ids)) != 3 or len(set(old_physician_ids)) != 3:
        return {'legal': False, 'reason': 'duplicate_rotation_member'}
    if len(set(new_physician_ids)) != 3 or set(new_physician_ids) != set(old_physician_ids):
        return {'legal': False, 'reason': 'rotation_must_preserve_physicians'}
    if any(
        instance_id in locked_open_instance_ids
        or (instance_id, physician_id) in manual_pairs
        or physician_id not in state.get(instance_id, ())
        for instance_id, physician_id in assignment_pairs
    ):
        return {'legal': False, 'reason': 'locked_or_missing_assignment'}
    if all(
        old_physician_id == new_physician_id
        for old_physician_id, new_physician_id in zip(
            old_physician_ids, new_physician_ids,
        )
    ):
        return {'legal': False, 'reason': 'noop_rotation'}
    for instance_id, old_physician_id, new_physician_id in zip(
        instance_ids, old_physician_ids, new_physician_ids,
    ):
        if (
            new_physician_id != old_physician_id
            and new_physician_id in state.get(instance_id, ())
        ):
            return {'legal': False, 'reason': 'duplicate_assignment'}

    trial_state = _copy_state(state)
    for instance_id, old_physician_id, new_physician_id in zip(
        instance_ids, old_physician_ids, new_physician_ids,
    ):
        _replace_in_state(
            trial_state, instance_id, old_physician_id, new_physician_id,
        )
    for instance_id, new_physician_id in zip(instance_ids, new_physician_ids):
        instance = instances_by_id[instance_id]
        if instance.facility_id not in eligible_facilities_by_physician.get(
            new_physician_id, set(),
        ):
            return {'legal': False, 'reason': 'not_facility_eligible'}
        intervals = _intervals_for_physician(
            trial_state, instances_by_id, new_physician_id,
            exclude_instance_id=instance_id,
        )
        if _overlaps(instance, intervals):
            return {'legal': False, 'reason': 'overlap'}
        if _rest_violation(
            instance, intervals,
            minimum_rest_by_physician[new_physician_id],
        ):
            return {'legal': False, 'reason': 'rest_violation'}

    incremental_delta = _selected_physician_score_delta(
        instances, physicians, state, trial_state, set(old_physician_ids),
        targets, contract_by_physician, requests_by_physician_date,
        eligible_facilities_by_physician, minimum_rest_by_physician,
    )
    if incremental_delta >= 0:
        return {
            'legal': True,
            'improving': False,
            'score_delta': incremental_delta,
            'incremental_score_delta': incremental_delta,
            'incremental_rejection': True,
            'scoring': None,
            'state': trial_state,
        }
    trial_scoring = _score_schedule(
        instances, physicians, trial_state, targets, contract_by_physician,
        requests_by_physician_date, eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    delta = trial_scoring['score'] - current_score
    return {
        'legal': True,
        'improving': delta < 0,
        'score_delta': delta,
        'incremental_score_delta': incremental_delta,
        'incremental_rejection': False,
        'scoring': trial_scoring,
        'state': trial_state,
    }


def recalculate_schedule_version_score(schedule_version, optimizer_run=None):
    """Refresh persisted scoring for the current assignments without optimizing."""
    report = build_violation_report(schedule_version, optimizer_run=optimizer_run)
    workload_summary = []
    for user in report['users']:
        workload = user.get('workload_score') or {}
        primary_rule = next(iter(workload.get('rule_rows') or []), None)
        workload_summary.append({
            'physician_id': user['user_id'],
            'physician_name': user['display_name'],
            'contract_name': workload.get('contract_name'),
            'contract_id': workload.get('contract_id'),
            'assigned_hours': user['hours'],
            'assigned_shifts': user['shifts'],
            'night_shifts': user['night_shifts'],
            'target_units': workload.get('target_units'),
            'target': workload.get('expected_target'),
            'raw_workload_rule': primary_rule,
            'effective_workload_range': _workload_range_from_rule(primary_rule),
            'deviation': workload.get('deviation'),
            'deviation_direction': workload.get('deviation_direction'),
            'score_contribution': workload.get('score_contribution', 0),
        })

    run = optimizer_run
    existing = dict((run.optimizer_summary if run else schedule_version.optimizer_summary) or {})
    debug = dict(existing.get('debug') or {})
    debug.update(report['debug'])
    debug['score_audit'] = report['score_audit']
    debug['score_recalculated_from_current_assignments'] = True
    breakdown = report['score_breakdown']
    summary = {
        **existing,
        'total_score': report['total_score'],
        'final_score': report['total_score'],
        'score_breakdown': breakdown,
        'unfilled_shift_count': int(breakdown.get('coverage_score', 0) / COVERAGE_PENALTY),
        'workload_summary': workload_summary,
        'debug': debug,
    }

    with transaction.atomic():
        version = ScheduleVersion.objects.select_for_update().get(id=schedule_version.id)
        version.optimizer_summary = summary
        version.score_is_stale = False
        version.save(update_fields=['optimizer_summary', 'score_is_stale', 'updated_at'])
        if run is not None:
            locked_run = OptimizerRun.objects.select_for_update().get(id=run.id)
            locked_run.final_score = report['total_score']
            locked_run.score_breakdown = breakdown
            locked_run.optimizer_summary = summary
            locked_run.optimizer_debug = debug
            locked_run.score_is_stale = False
            locked_run.save(update_fields=[
                'final_score', 'score_breakdown', 'optimizer_summary',
                'optimizer_debug', 'score_is_stale',
            ])
    return summary, report


def optimize_schedule_version(
    schedule_version,
    created_by=None,
    optimizer_run=None,
    seed=None,
    start_mode=OptimizerRun.StartMode.FRESH_FILL,
    source_run=None,
    run_kind='OPTIMIZER',
    allow_preview_benchmark=False,
    adaptive_runtime=False,
    stop_requested=None,
    max_runtime_seconds=None,
    progress_callback=None,
):
    _FULL_SCORE_EVALUATIONS.set(0)
    _SCORE_CACHE.set(OrderedDict())
    _SCORE_CACHE_HITS.set(0)
    _SCORE_CACHE_MISSES.set(0)
    _SCORE_CACHE_EVICTIONS.set(0)
    invocation_started_at = monotonic()
    runtime_limit_seconds = int(
        MAX_RUNTIME_SECONDS if max_runtime_seconds is None else max_runtime_seconds
    )
    if not 0 <= runtime_limit_seconds <= 4 * 60 * 60:
        raise ValueError('Maximum optimizer runtime cannot exceed 240 minutes.')
    if schedule_version.status != ScheduleVersion.Status.BUILD:
        raise ValueError('Optimizer can only run on a BUILD Schedule Version.')
    allowed_schedule_block_statuses = (ScheduleBlock.BuildStatus.BUILD,)
    if allow_preview_benchmark:
        if run_kind != 'BENCHMARK':
            raise ValueError('PREVIEW optimizer access is reserved for BENCHMARK runs.')
        allowed_schedule_block_statuses = (
            ScheduleBlock.BuildStatus.BUILD,
            ScheduleBlock.BuildStatus.PREVIEW,
        )
    if schedule_version.schedule_block.build_status not in allowed_schedule_block_statuses:
        allowed_labels = ' or '.join(allowed_schedule_block_statuses)
        raise ValueError(
            f'Optimizer can only run while the Schedule Block is in {allowed_labels}.'
        )

    with transaction.atomic():
        version = (
            ScheduleVersion.objects.select_for_update()
            .select_related('schedule_block', 'domain')
            .get(id=schedule_version.id)
        )
        if start_mode not in OptimizerRun.StartMode.values:
            raise ValueError('Invalid optimizer start mode.')
        if source_run is not None:
            source_run = OptimizerRun.objects.select_for_update().get(
                id=source_run.id,
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
            )
        source_locked_open_ids = (
            list(source_run.locked_open_shift_instance_ids or [])
            if source_run is not None
            else list(
                ScheduleShiftInstance.objects.filter(
                    schedule_version=version, is_locked_open=True,
                ).values_list('id', flat=True)
            )
        )
        if optimizer_run is None:
            latest_run_number = (
                OptimizerRun.objects.filter(schedule_version=version)
                .order_by('-run_number')
                .values_list('run_number', flat=True)
                .first()
                or 0
            )
            if seed is None:
                seed = secrets.randbits(63)
            optimizer_run = OptimizerRun.objects.create(
                schedule_version=version,
                run_number=latest_run_number + 1,
                created_by=created_by,
                status=OptimizerRun.Status.RUNNING,
                seed=seed,
                start_mode=start_mode,
                started_from_run=(
                    source_run
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    else None
                ),
                started_from_run_number=(
                    source_run.run_number
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    and source_run is not None
                    else None
                ),
                initial_score=(
                    source_run.final_score
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    and source_run is not None
                    else None
                ),
                max_runtime_seconds=runtime_limit_seconds,
                run_kind=run_kind,
                locked_open_shift_instance_ids=source_locked_open_ids,
            )
        else:
            optimizer_run = OptimizerRun.objects.select_for_update().get(
                id=optimizer_run.id,
                schedule_version=version,
            )
            expected_started_from_run = (
                source_run
                if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                else None
            )
            expected_started_from_number = (
                source_run.run_number
                if expected_started_from_run is not None
                else None
            )
            lineage_updates = []
            if optimizer_run.started_from_run_id != (
                expected_started_from_run.id if expected_started_from_run else None
            ):
                optimizer_run.started_from_run = expected_started_from_run
                lineage_updates.append('started_from_run')
            if optimizer_run.started_from_run_number != expected_started_from_number:
                optimizer_run.started_from_run_number = expected_started_from_number
                lineage_updates.append('started_from_run_number')
            if (
                expected_started_from_run is not None
                and optimizer_run.initial_score != expected_started_from_run.final_score
            ):
                optimizer_run.initial_score = expected_started_from_run.final_score
                lineage_updates.append('initial_score')
            if lineage_updates:
                optimizer_run.save(update_fields=lineage_updates)
            runtime_limit_seconds = int(
                max_runtime_seconds
                if max_runtime_seconds is not None
                else optimizer_run.max_runtime_seconds
            )
            if not 0 <= runtime_limit_seconds <= 4 * 60 * 60:
                raise ValueError('Maximum optimizer runtime cannot exceed 240 minutes.')
            if optimizer_run.max_runtime_seconds != runtime_limit_seconds:
                optimizer_run.max_runtime_seconds = runtime_limit_seconds
                optimizer_run.save(update_fields=['max_runtime_seconds'])
            if seed is not None and optimizer_run.seed != seed:
                optimizer_run.seed = seed
                optimizer_run.save(update_fields=['seed'])
        if optimizer_run.seed is None:
            optimizer_run.seed = seed if seed is not None else secrets.randbits(63)
            optimizer_run.save(update_fields=['seed'])
        seed = optimizer_run.seed
        rng = random.Random(seed)

        def random_tie_sorted(items, key):
            decorated = [
                (key(item), rng.random(), index, item)
                for index, item in enumerate(items)
            ]
            decorated.sort(key=lambda row: (row[0], row[1], row[2]))
            return [item for _key, _tie, _index, item in decorated]

        def shuffle(items):
            values = list(items)
            rng.shuffle(values)
            return values

        assignment_rows_before = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            shift_instance__date__gte=version.schedule_block.start_date,
            shift_instance__date__lte=version.schedule_block.end_date,
        ).count()
        optimizer_assignments_deleted = 0
        ScheduleShiftInstance.objects.filter(schedule_version=version).update(is_locked_open=False)
        ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            id__in=source_locked_open_ids,
        ).update(is_locked_open=True)
        instances = list(
            _version_shift_instances_queryset(version)
            .select_for_update()
            .select_related('facility', 'shift_template')
            .order_by('date', 'facility__name', 'start_datetime', 'id')
        )
        if source_run is not None:
            raw_source_assignments = list(
                assignments_for_viewed_run(version, source_run)
                .select_related('shift_instance', 'physician__user')
            )
        else:
            raw_source_assignments = list(
                assignments_for_viewed_run(version, None)
                .select_related('shift_instance', 'physician__user')
            )
        manual_assignment_only_physician_ids = set(
            ContractUserAssignment.objects.filter(
                domain=version.domain,
                contract__active=True,
                contract__manual_assignment_only=True,
                physician__active=True,
            ).values_list('physician_id', flat=True)
        )
        source_assignments, source_assignment_normalization = canonical_assignment_snapshot(
            raw_source_assignments,
            instances,
            selected_run=source_run,
            preserve_physician_ids=manual_assignment_only_physician_ids,
        )
        # A physician can be switched to a manual-only contract after an older
        # optimizer run assigned them shifts.  Those optimizer-owned rows must
        # not become frozen placeholder assignments in every later run.  Only
        # assignments explicitly made by a scheduler are authoritative for a
        # manual-only physician.
        manual_only_optimizer_source_rows_dropped = sum(
            1
            for row in source_assignments
            if (
                row.physician_id in manual_assignment_only_physician_ids
                and row.assignment_source == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
            )
        )
        source_assignments = [
            row
            for row in source_assignments
            if not (
                row.physician_id in manual_assignment_only_physician_ids
                and row.assignment_source == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
            )
        ]
        source_assignment_count_raw = len(raw_source_assignments)

        if source_assignments:
            source_assignment_count = len(source_assignments)
            # Self-contained runs cannot rely on the runless manual overlay:
            # their authoritative view includes only rows owned by this run.
            if source_run is None and optimizer_run.run_kind not in ('COPY', 'BENCHMARK'):
                assignments = [
                    row for row in source_assignments
                    if (
                        start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                        or row.is_locked
                        or row.physician_id in manual_assignment_only_physician_ids
                    )
                ]
            else:
                manual_seed_rows_by_pair = {}
                manual_overlay_rows = []
                for row in source_assignments:
                    if (
                        row.assignment_source != ScheduleShiftAssignment.AssignmentSource.MANUAL
                        or not (
                            start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                            or row.is_locked
                        )
                    ):
                        continue
                    if row.optimizer_run_id is None and optimizer_run.run_kind not in ('COPY', 'BENCHMARK'):
                        # Normal runs already display runless manual assignments.
                        # Copying them would show the same locked slot twice.
                        manual_overlay_rows.append(row)
                        continue
                    pair = (row.shift_instance_id, row.physician_id)
                    manual_seed_rows_by_pair[pair] = ScheduleShiftAssignment(
                        shift_instance_id=row.shift_instance_id,
                        physician_id=row.physician_id,
                        created_by=created_by,
                        assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
                        optimizer_run=optimizer_run,
                        is_locked=row.is_locked,
                    )
                manual_seed_rows = list(manual_seed_rows_by_pair.values())
                ScheduleShiftAssignment.objects.bulk_create(manual_seed_rows)
                assignments = (
                    [
                        row for row in source_assignments
                        if (
                            row.assignment_source == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
                            and (
                                start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                                or row.physician_id in manual_assignment_only_physician_ids
                            )
                        )
                    ]
                    if source_assignments
                    else []
                ) + manual_overlay_rows + list(
                    ScheduleShiftAssignment.objects.filter(
                        optimizer_run=optimizer_run,
                        assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
                    ).select_related('shift_instance', 'physician__user')
                )
        else:
            source_assignment_count = 0
            assignments = []
        manual_assignments_preserved = sum(
            1
            for assignment in assignments
            if assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
        )
        duplicate_shift_instances = list(
            _version_shift_instances_queryset(version)
            .values('date', 'shift_template_id')
            .annotate(row_count=Count('id'))
            .filter(row_count__gt=1)
        )

        active_contract_assignments = list(
            ContractUserAssignment.objects.filter(
                domain=version.domain,
                contract__active=True,
                physician__active=True,
            )
            .select_related('physician__user', 'contract')
            .prefetch_related('contract__facilities')
            .order_by('physician__user__last_name', 'physician__user__first_name', 'physician_id')
        )
        physicians = [assignment.physician for assignment in active_contract_assignments]
        physicians_by_id = {physician.id: physician for physician in physicians}
        contract_by_physician = {
            assignment.physician_id: assignment.contract
            for assignment in active_contract_assignments
        }
        scoring_physicians = [
            physician
            for physician in physicians
            if physician.id not in manual_assignment_only_physician_ids
        ]
        scoring_contract_by_physician = {
            physician_id: contract
            for physician_id, contract in contract_by_physician.items()
            if physician_id not in manual_assignment_only_physician_ids
        }
        published_boundary_context = _attach_published_boundary_context(
            version,
            instances,
            scoring_contract_by_physician,
        )
        minimum_rest_by_physician = {
            assignment.physician_id: _minimum_rest_hours(assignment.contract)
            for assignment in active_contract_assignments
        }
        eligible_facilities_by_physician = {
            assignment.physician_id: {
                facility.id for facility in assignment.contract.facilities.all()
            }
            for assignment in active_contract_assignments
        }
        for physician_id in manual_assignment_only_physician_ids:
            eligible_facilities_by_physician[physician_id] = set()

        state, manual_pairs = _state_from_assignments(assignments)
        runless_manual_overlay_pairs = {
            (assignment.shift_instance_id, assignment.physician_id)
            for assignment in assignments
            if (
                assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
                and assignment.optimizer_run_id is None
            )
        }
        manual_pairs.update(
            (instance_id, physician_id)
            for instance_id, physician_ids in state.items()
            for physician_id in physician_ids
            if physician_id in manual_assignment_only_physician_ids
        )
        source_visible_assignment_pairs = {
            (assignment.shift_instance_id, assignment.physician_id)
            for assignment in source_assignments
        }
        loaded_start_assignment_pairs = {
            (instance_id, physician_id)
            for instance_id, physician_ids in state.items()
            for physician_id in physician_ids
        }
        assignments_same_at_start = (
            loaded_start_assignment_pairs == source_visible_assignment_pairs
        )
        source_pairs_missing_at_start = sorted(
            source_visible_assignment_pairs - loaded_start_assignment_pairs
        )
        source_pairs_extra_at_start = sorted(
            loaded_start_assignment_pairs - source_visible_assignment_pairs
        )
        instances_by_id = {instance.id: instance for instance in instances}

        requests = (
            ScheduleRequest.objects.filter(
                schedule_block=version.schedule_block,
                date__gte=version.schedule_block.start_date,
                date__lte=version.schedule_block.end_date,
            )
            .prefetch_related('shift_templates')
        )
        requests_by_physician_date = defaultdict(list)
        for schedule_request in requests:
            requests_by_physician_date[
                (schedule_request.physician_id, schedule_request.date)
            ].append(schedule_request)

        # For a manual-only physician, a Shift On request is the scheduler's
        # authoritative assignment instruction regardless of scope or weight.
        # Seed it before any workload accounting or search, lock it like a
        # manual assignment, and remove only replaceable optimizer occupants.
        instances_by_date_template = defaultdict(list)
        for instance in instances:
            instances_by_date_template[
                (instance.date, instance.shift_template_id)
            ].append(instance)
        manual_only_request_assignment_rows = []
        manual_only_request_assignments_seeded = 0
        manual_only_request_assignments_already_present = 0
        manual_only_request_optimizer_owners_displaced = 0
        manual_only_request_conflicts = []
        for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
            if physician_id not in manual_assignment_only_physician_ids:
                continue
            for schedule_request in schedule_requests:
                if schedule_request.request_type != ScheduleRequest.RequestType.SHIFT_ON:
                    continue
                matching_instances = []
                for template in schedule_request.shift_templates.all():
                    matching_instances.extend(
                        instances_by_date_template.get((request_date, template.id), ())
                    )
                matching_instances.sort(
                    key=lambda item: (item.start_datetime, item.end_datetime, item.id)
                )
                if not matching_instances:
                    manual_only_request_conflicts.append({
                        'request_id': schedule_request.id,
                        'physician_id': physician_id,
                        'date': request_date.isoformat(),
                        'reason': 'no_matching_shift_instance',
                    })
                    continue
                instance = matching_instances[0]
                pair = (instance.id, physician_id)
                if physician_id in state[instance.id]:
                    manual_pairs.add(pair)
                    manual_only_request_assignments_already_present += 1
                    continue

                replaceable_owner_ids = [
                    owner_id
                    for owner_id in state[instance.id]
                    if (instance.id, owner_id) not in manual_pairs
                ]
                while (
                    len(state[instance.id]) >= instance.required_staffing
                    and replaceable_owner_ids
                ):
                    owner_id = replaceable_owner_ids.pop()
                    state[instance.id].remove(owner_id)
                    manual_only_request_optimizer_owners_displaced += 1
                if len(state[instance.id]) >= instance.required_staffing:
                    manual_only_request_conflicts.append({
                        'request_id': schedule_request.id,
                        'physician_id': physician_id,
                        'date': request_date.isoformat(),
                        'shift_instance_id': instance.id,
                        'reason': 'conflicts_with_existing_fixed_assignment',
                    })

                _add_to_state(state, instance.id, physician_id)
                manual_pairs.add(pair)
                manual_only_request_assignment_rows.append(
                    ScheduleShiftAssignment(
                        shift_instance=instance,
                        physician_id=physician_id,
                        created_by=created_by,
                        assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
                        optimizer_run=optimizer_run,
                        is_locked=True,
                    )
                )
                manual_only_request_assignments_seeded += 1
        if manual_only_request_assignment_rows:
            ScheduleShiftAssignment.objects.bulk_create(
                manual_only_request_assignment_rows,
                batch_size=500,
            )
            assignments.extend(manual_only_request_assignment_rows)

        total_required_hours = sum(
            _shift_hours(instance) * instance.required_staffing
            for instance in instances
        )
        total_required_slots = sum(
            instance.required_staffing
            for instance in instances
        )
        default_hours_target = (
            total_required_hours / Decimal(len(physicians))
            if physicians
            else Decimal('0')
        )
        default_shift_target = (
            Decimal(total_required_slots) / Decimal(len(physicians))
            if physicians
            else Decimal('0')
        )
        targets = {
            physician.id: _version_contract_target(
                version, physician.id, contract_by_physician[physician.id],
                default_hours_target,
                default_shift_target,
            )
            for physician in physicians
        }
        for physician_id in manual_assignment_only_physician_ids:
            targets[physician_id] = {
                'units': 'HOURS',
                'target': Decimal('0'),
                'minimum': Decimal('0'),
                'maximum': Decimal('0'),
                'rules': [],
            }
        workload_ranges_by_physician = {}
        workload_totals_by_physician = {}
        night_ranges_by_physician = {}
        night_totals_by_physician = {}
        for physician in physicians:
            range_rows = []
            values = defaultdict(lambda: Decimal('0'))
            for rule in targets[physician.id].get('rules') or []:
                for window_start, window_end in _period_windows(instances, rule['period_type']):
                    effective_rule = _effective_workload_rule(rule, window_start, window_end)
                    range_rows.append({
                        **effective_rule,
                        'window_start': window_start,
                        'window_end': window_end,
                    })
            for instance in instances:
                if physician.id not in state[instance.id]:
                    continue
                for row in range_rows:
                    if row['window_start'] <= instance.date <= row['window_end']:
                        key = (row['window_start'], row['window_end'], row['units'])
                        values[key] += Decimal('1') if row['units'] == 'SHIFTS' else _shift_hours(instance)
            workload_ranges_by_physician[physician.id] = range_rows
            workload_totals_by_physician[physician.id] = values

            night_range_rows = []
            night_values = defaultdict(lambda: Decimal('0'))
            for rule in _unique_night_period_rules(_night_settings(contract_by_physician[physician.id])):
                min_shifts = _decimal_or_none(rule.get('min_shifts'))
                max_shifts = _decimal_or_none(rule.get('max_shifts'))
                if min_shifts is None and max_shifts is None:
                    continue
                for window_start, window_end in _period_windows(instances, rule.get('period_type') or 'SCHEDULE_BLOCK'):
                    night_range_rows.append({
                        'window_start': window_start,
                        'window_end': window_end,
                        'min_shifts': min_shifts,
                        'max_shifts': max_shifts,
                        'min_penalty_weight': _configured_positive_penalty(
                            rule, 'min_penalty_weight', DEFAULT_NIGHT_BALANCE_PENALTY,
                        ),
                        'max_penalty_weight': _configured_positive_penalty(
                            rule, 'max_penalty_weight', DEFAULT_NIGHT_BALANCE_PENALTY,
                        ),
                    })
            for assigned_instance in instances:
                if (
                    physician.id not in state[assigned_instance.id]
                    or not assigned_instance.shift_template.night_shift
                ):
                    continue
                for row in night_range_rows:
                    if row['window_start'] <= assigned_instance.date <= row['window_end']:
                        night_values[(row['window_start'], row['window_end'])] += Decimal('1')
            night_ranges_by_physician[physician.id] = night_range_rows
            night_totals_by_physician[physician.id] = night_values

        fixed_request_diagnostic = _fixed_shift_on_request_workload_floor(
            instances,
            physicians,
            targets,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            manual_assignment_only_physician_ids=manual_assignment_only_physician_ids,
        )
        proven_score_floor = Decimal(fixed_request_diagnostic['score_floor_exact'])

        source_state_scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
            include_internal_night_heuristics=True,
        )
        source_state_score_before_pre_score_changes = source_state_scoring['score']
        source_state_reported_scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
            include_internal_night_heuristics=False,
        )
        source_state_reported_score_before_pre_score_changes = (
            source_state_reported_scoring['score']
        )
        source_state_snapshot = _copy_state(state)
        source_state_restored_as_best = False
        assignment_pairs_before_pre_score_changes = set(loaded_start_assignment_pairs)

        assignments_made = 0
        rest_violations_blocked = 0
        night_block_assignment_attempts = 0
        night_block_assignment_successes = 0
        night_block_builder_candidates_created = 0
        night_block_builder_rejections_by_reason = defaultdict(int)
        night_block_builder_assigned_blocks = []
        night_block_builder_lengths_assigned = []
        night_block_builder_score_before = None
        night_block_builder_score_after = None
        physicians_below_night_min_before_night_build = []
        physicians_below_night_min_after_night_build = []
        night_recovery_conflicts_after_night_build = 0
        night_distribution_by_physician_after_build = []
        nonnight_assignments_blocked_by_recovery = 0
        nonnight_assignments_allowed_despite_recovery = 0
        open_slots_available = _unfilled_slot_count(instances, state)
        optimizer_search_started_at = monotonic()
        search_budget_kwargs = dict(
            # The advertised maximum applies to the entire optimizer
            # invocation, including setup. Starting the absolute budget only
            # after setup could consume the worker's finalization margin and
            # cause a valid best state to be rolled back by the safety alarm.
            started_at=invocation_started_at,
            score_floor=proven_score_floor,
            stop_requested=stop_requested or (lambda: False),
        )
        # Keep the constructor's default overridable by benchmark/test budget
        # hooks for direct calls. API-selected and queued-run limits are
        # explicit and therefore authoritative.
        if max_runtime_seconds is not None:
            search_budget_kwargs['total_seconds'] = runtime_limit_seconds
        search_budget = SearchBudget(**search_budget_kwargs)
        adaptive_best_state = _copy_state(source_state_snapshot)
        adaptive_best_scoring = source_state_reported_scoring
        progress_ready = False
        progress_marker = None
        adaptive_debug = {'enabled': adaptive_runtime, 'cycles': 0, 'improvements': 0,
                          'attempts': 0, 'stall_seconds': search_budget.stall_seconds,
                          'overall_seconds': search_budget.total_seconds,
                          'proven_score_floor': float(proven_score_floor)}
        # Once construction has produced a complete valid schedule, reserve
        # most of the remaining runtime for the adaptive portfolio and bounded
        # reconstruction neighborhoods.  Without this handoff, a large fresh
        # fill can consume the entire budget in single-move polishing.
        adaptive_handoff_seconds = min(
            600.0,
            max(30.0, float(search_budget.total_seconds) * 0.40),
        )
        adaptive_continuation_started = False
        adaptive_debug['primary_phase_budget_seconds'] = adaptive_handoff_seconds

        def observe_best(candidate_state, candidate_scoring):
            nonlocal adaptive_best_state, adaptive_best_scoring
            candidate_priority = _state_result_priority(
                instances, candidate_state, candidate_scoring,
            )
            valid = (candidate_priority[0] == 0
                     and candidate_scoring.get('fixed_request_on_unmet', 0) == 0
                     and all(pid in candidate_state[sid] for sid, pid in manual_pairs))
            search_budget.observe(candidate_scoring['score'], valid=valid)
            if (
                valid
                and progress_callback is not None
                and search_budget.best_score is not None
            ):
                progress_callback(search_budget.best_score)
            if (
                valid
                and candidate_priority
                < _state_result_priority(
                    instances, adaptive_best_state, adaptive_best_scoring,
                )
            ):
                adaptive_best_state = _copy_state(candidate_state)
                adaptive_best_scoring = candidate_scoring
                return True
            return False

        # A run must search only from the starting point selected by the
        # scheduler.  Historical runs remain available as explicit starting
        # choices, but they must not silently replace a fresh-fill or another
        # run's independent result.
        historical_elite_enabled = False
        if adaptive_runtime:
            observe_best(state, source_state_reported_scoring)
            adaptive_debug['elite_archive'] = {
                'enabled': False,
                'reason': 'Runs retain independent results; historical runs are explicit starting points.',
            }
        if adaptive_runtime and historical_elite_enabled:
            observe_best(state, source_state_reported_scoring)
            # Preserve a small, diverse elite archive across runs. A fresh
            # fill is still constructed and searched independently, but it
            # must not forget a substantially better basin that remains valid
            # under the current instances, contracts, requests, manual locks,
            # and fixed Request On assignments. Historical scores are never
            # trusted: every candidate is normalized and fully rescored now.
            elite_debug = {
                'candidates_considered': 0,
                'valid_candidates': 0,
                'selected_run_id': None,
                'selected_run_number': None,
                'selected_score': None,
                'rejections': defaultdict(int),
                'candidate_details': [],
            }
            prior_runs = OptimizerRun.objects.filter(
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
                run_kind='OPTIMIZER',
            ).exclude(id=optimizer_run.id)
            lowest_score_runs = list(prior_runs.order_by(
                'final_score', '-created_at', 'id',
            )[:12])
            recent_runs = list(prior_runs.order_by('-created_at', '-id')[:4])
            elite_runs = []
            seen_elite_run_ids = set()
            for prior_run in [*lowest_score_runs, *recent_runs]:
                if prior_run.id in seen_elite_run_ids:
                    continue
                seen_elite_run_ids.add(prior_run.id)
                elite_runs.append(prior_run)

            for prior_run in elite_runs:
                elite_debug['candidates_considered'] += 1
                detail = {
                    'run_id': prior_run.id,
                    'run_number': prior_run.run_number,
                    'stored_score': float(prior_run.final_score or 0),
                }
                if set(prior_run.locked_open_shift_instance_ids or ()) != set(
                    source_locked_open_ids
                ):
                    elite_debug['rejections']['locked_open_mismatch'] += 1
                    detail['rejected_reason'] = 'locked_open_mismatch'
                    elite_debug['candidate_details'].append(detail)
                    continue
                historical_rows = list(
                    assignments_for_viewed_run(version, prior_run)
                    .select_related('shift_instance', 'physician__user')
                )
                normalized_rows, normalization = canonical_assignment_snapshot(
                    historical_rows,
                    instances,
                    selected_run=prior_run,
                    preserve_physician_ids=manual_assignment_only_physician_ids,
                )
                normalized_rows = [
                    row for row in normalized_rows
                    if not (
                        row.physician_id in manual_assignment_only_physician_ids
                        and row.assignment_source
                        == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
                    )
                ]
                candidate_state, _candidate_manual_pairs = _state_from_assignments(
                    normalized_rows
                )
                if not all(
                    physician_id in candidate_state[instance_id]
                    for instance_id, physician_id in manual_pairs
                ):
                    elite_debug['rejections']['manual_assignment_mismatch'] += 1
                    detail['rejected_reason'] = 'manual_assignment_mismatch'
                    elite_debug['candidate_details'].append(detail)
                    continue
                candidate_scoring = _score_schedule(
                    instances,
                    physicians,
                    candidate_state,
                    targets,
                    contract_by_physician,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    include_internal_night_heuristics=False,
                )
                candidate_priority = _state_result_priority(
                    instances, candidate_state, candidate_scoring,
                )
                detail.update({
                    'current_score': float(candidate_scoring['score']),
                    'unfilled_shift_count': _unfilled_slot_count(
                        instances, candidate_state,
                    ),
                    'fixed_request_on_unmet': int(
                        candidate_scoring.get('fixed_request_on_unmet', 0)
                    ),
                    'normalization': normalization,
                })
                if candidate_priority[0] != 0:
                    elite_debug['rejections']['incomplete_or_invalid'] += 1
                    detail['rejected_reason'] = 'incomplete_or_invalid'
                elif candidate_scoring.get('fixed_request_on_unmet', 0):
                    elite_debug['rejections']['fixed_request_on_unmet'] += 1
                    detail['rejected_reason'] = 'fixed_request_on_unmet'
                else:
                    elite_debug['valid_candidates'] += 1
                    selected = observe_best(candidate_state, candidate_scoring)
                    detail['selected'] = selected
                    if selected:
                        elite_debug['selected_run_id'] = prior_run.id
                        elite_debug['selected_run_number'] = prior_run.run_number
                        elite_debug['selected_score'] = float(
                            candidate_scoring['score']
                        )
                elite_debug['candidate_details'].append(detail)
            elite_debug['rejections'] = dict(elite_debug['rejections'])
            adaptive_debug['elite_archive'] = elite_debug
            search_budget.observe_coverage(0)
        timed_out = False
        phase_running_when_stopped = None
        candidates_considered_before_timeout = 0
        initial_fill_workload_guard_candidates_above_max = 0
        initial_fill_workload_guard_candidates_deprioritized = 0
        initial_fill_workload_guard_examples = []

        night_instances = [
            instance for instance in instances
            if instance.shift_template.night_shift
        ]
        non_night_instances = [
            instance for instance in instances
            if not instance.shift_template.night_shift
        ]

        def night_recovery_conflict_count(report):
            return sum(
                1
                for violation in report['night_violations']
                if violation['violation_type'] in (
                    'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT',
                    'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK',
                )
            )

        def night_distribution_rows(report):
            return [
                {
                    'physician_id': row['physician_id'],
                    'physician': row['physician'],
                    'night_shifts': row['night_shifts'],
                }
                for row in report['night_shifts_by_physician']
            ]

        def runtime_seconds_elapsed():
            return monotonic() - optimizer_search_started_at

        def runtime_exceeded():
            nonlocal progress_marker
            if adaptive_runtime:
                if (
                    progress_callback is not None
                    and search_budget.best_score is not None
                ):
                    progress_callback(search_budget.best_score)
                if not progress_ready:
                    # During construction, accepted assignments are progress
                    # even though no complete schedule can yet be scored.
                    search_budget.observe_coverage(assignments_made)
                    return search_budget.reason() is not None
                if progress_ready and final_score != progress_marker:
                    progress_marker = final_score
                    observe_best(state, _score_schedule(
                        instances, physicians, state, targets, contract_by_physician,
                        requests_by_physician_date, eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                    ))
                if search_budget.reason() is not None:
                    return True
                if (
                    progress_ready
                    and not adaptive_continuation_started
                    and runtime_seconds_elapsed() >= adaptive_handoff_seconds
                ):
                    adaptive_debug['primary_phase_handoff'] = True
                    return True
                return False
            return runtime_seconds_elapsed() >= runtime_limit_seconds

        def mark_timeout(phase):
            nonlocal timed_out
            nonlocal stopped_reason
            nonlocal phase_running_when_stopped
            timed_out = True
            stopped_reason = 'runtime_limit'
            if phase_running_when_stopped is None:
                phase_running_when_stopped = phase
            return True

        def night_rule_window_deficit(status_rows):
            return sum(
                max(row['minimum'] - row['actual'], 0)
                for row in status_rows
            )

        def block_candidate_lengths(physician_id, available_instances):
            contract = contract_by_physician.get(physician_id)
            settings = _night_settings(contract) if contract is not None else {}
            min_consecutive = _configured_positive_int(
                settings,
                'min_consecutive_night_shifts',
            ) or 1
            max_consecutive = (
                _configured_positive_int(
                    settings,
                    'max_consecutive_night_shifts',
                )
                or len(available_instances)
            )
            max_feasible = min(max_consecutive, len(available_instances))
            preferred = [
                length
                for length in range(max_feasible, 0, -1)
                if min_consecutive <= length <= max_consecutive
            ]
            fallback = [
                length
                for length in range(max_feasible, 0, -1)
                if length < min_consecutive
            ]
            return preferred + fallback

        def consecutive_night_windows(ordered_night_instances):
            windows = []
            ordered = sorted(
                ordered_night_instances,
                key=lambda item: (item.date, item.facility.name, item.start_datetime, item.id),
            )
            for start_index, start_instance in enumerate(ordered):
                current = [start_instance]
                windows.append(list(current))
                previous = start_instance
                for next_instance in ordered[start_index + 1:]:
                    if next_instance.date != previous.date + timedelta(days=1):
                        break
                    current.append(next_instance)
                    windows.append(list(current))
                    previous = next_instance
            return windows

        def build_night_blocks():
            nonlocal assignments_made
            nonlocal rest_violations_blocked
            nonlocal night_block_assignment_attempts
            nonlocal night_block_assignment_successes
            nonlocal night_block_builder_candidates_created
            nonlocal night_block_builder_score_before
            nonlocal night_block_builder_score_after
            nonlocal physicians_below_night_min_before_night_build
            nonlocal physicians_below_night_min_after_night_build
            nonlocal night_recovery_conflicts_after_night_build
            nonlocal night_distribution_by_physician_after_build

            before_scoring = _score_schedule(
                instances,
                physicians,
                state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            night_block_builder_score_before = float(before_scoring['score'])
            before_status = _night_minimum_status(
                instances,
                physicians,
                state,
                contract_by_physician,
            )
            physicians_below_night_min_before_night_build = before_status['physicians_under_night_minimum']

            while True:
                if runtime_exceeded():
                    mark_timeout('night_block_builder')
                    break
                unfilled_nights = [
                    instance for instance in night_instances
                    if len(state[instance.id]) < instance.required_staffing
                ]
                if not unfilled_nights:
                    break

                minimum_status = _night_minimum_status(
                    instances,
                    physicians,
                    state,
                    contract_by_physician,
                )
                under_minimum_ids = {
                    row['physician_id']
                    for row in minimum_status['physicians_under_night_minimum']
                }
                current_under_deficit = night_rule_window_deficit(
                    minimum_status['physicians_under_night_minimum']
                )
                candidates = []
                windows = shuffle(consecutive_night_windows(unfilled_nights))
                for physician in shuffle(physicians):
                    if runtime_exceeded():
                        mark_timeout('night_block_builder')
                        break
                    physician_windows = shuffle(windows)
                    for window in physician_windows:
                        if runtime_exceeded():
                            mark_timeout('night_block_builder')
                            break
                        for length in block_candidate_lengths(physician.id, window):
                            if runtime_exceeded():
                                mark_timeout('night_block_builder')
                                break
                            block = window[:length]
                            if not block:
                                continue
                            night_block_assignment_attempts += 1
                            trial_state = _copy_state(state)
                            rejected = None
                            for instance in block:
                                if len(trial_state[instance.id]) >= instance.required_staffing:
                                    rejected = 'filled'
                                    break
                                if physician.id in trial_state[instance.id]:
                                    rejected = 'duplicate'
                                    break
                                if instance.facility_id not in eligible_facilities_by_physician.get(physician.id, set()):
                                    rejected = 'facility_ineligible'
                                    break
                                if not _can_assign_in_state(
                                    trial_state,
                                    instances_by_id,
                                    instance,
                                    physician.id,
                                    eligible_facilities_by_physician,
                                    minimum_rest_by_physician,
                                ):
                                    rejected = 'rest_or_overlap'
                                    rest_violations_blocked += 1
                                    break
                                _add_to_state(trial_state, instance.id, physician.id)
                            if rejected is not None:
                                night_block_builder_rejections_by_reason[rejected] += 1
                                continue

                            night_block_builder_candidates_created += 1
                            trial_scoring = _score_schedule(
                                instances,
                                physicians,
                                trial_state,
                                targets,
                                contract_by_physician,
                                requests_by_physician_date,
                                eligible_facilities_by_physician,
                                minimum_rest_by_physician,
                                include_internal_night_heuristics=True,
                            )
                            trial_status = _night_minimum_status(
                                instances,
                                physicians,
                                trial_state,
                                contract_by_physician,
                            )
                            trial_report = _night_violation_report(
                                instances,
                                physicians,
                                trial_state,
                                contract_by_physician,
                            )
                            trial_under_deficit = night_rule_window_deficit(
                                trial_status['physicians_under_night_minimum']
                            )
                            candidates.append(
                                (
                                    0 if physician.id in under_minimum_ids else 1,
                                    trial_under_deficit,
                                    night_recovery_conflict_count(trial_report),
                                    -len(block),
                                    trial_scoring['score'],
                                    rng.random(),
                                    physician,
                                    block,
                                    trial_state,
                                )
                            )

                if not candidates:
                    break

                under_candidates = [
                    candidate for candidate in candidates
                    if candidate[0] == 0
                ]
                candidate_pool = under_candidates or candidates
                improving_minimum_candidates = [
                    candidate for candidate in candidate_pool
                    if candidate[1] < current_under_deficit
                ]
                if improving_minimum_candidates:
                    candidate_pool = improving_minimum_candidates

                (
                    _under_priority,
                    _trial_under_deficit,
                    _recovery_conflicts,
                    _negative_length,
                    _trial_score,
                    _tie_breaker,
                    selected_physician,
                    selected_block,
                    selected_state,
                ) = min(candidate_pool)
                state.clear()
                state.update(selected_state)
                assignments_made += len(selected_block)
                night_block_assignment_successes += len(selected_block)
                night_block_builder_lengths_assigned.append(len(selected_block))
                night_block_builder_assigned_blocks.append(
                    {
                        'physician_id': selected_physician.id,
                        'physician': _physician_display_name(selected_physician),
                        **_contract_rule_identity(contract_by_physician.get(selected_physician.id)),
                        'length': len(selected_block),
                        'dates': _block_dates(selected_block),
                        'shift_instance_ids': [instance.id for instance in selected_block],
                        'facilities': sorted({
                            instance.facility.short_name or instance.facility.name
                            for instance in selected_block
                        }),
                    }
                )

            after_scoring = _score_schedule(
                instances,
                physicians,
                state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            night_block_builder_score_after = float(after_scoring['score'])
            after_status = _night_minimum_status(
                instances,
                physicians,
                state,
                contract_by_physician,
            )
            physicians_below_night_min_after_night_build = after_status['physicians_under_night_minimum']
            after_report = _night_violation_report(
                instances,
                physicians,
                state,
                contract_by_physician,
            )
            night_recovery_conflicts_after_night_build = night_recovery_conflict_count(after_report)
            night_distribution_by_physician_after_build = night_distribution_rows(after_report)

        initial_fill_hours = defaultdict(lambda: Decimal('0'))
        initial_fill_shifts = defaultdict(int)
        initial_fill_template_indexes = defaultdict(list)
        initial_fill_intervals = defaultdict(_AssignmentIntervalIndex)
        initial_fill_night_dates = defaultdict(set)
        initial_fill_weekend_dates = defaultdict(set)
        initial_fill_weekend_weeks = defaultdict(set)
        initial_fill_assigned_dates = defaultdict(set)
        initial_fill_template_positions = _template_occurrence_positions(instances)
        initial_fill_open_capacity = defaultdict(lambda: defaultdict(lambda: Decimal('0')))
        initial_fill_opportunity_windows = set()
        if start_mode == OptimizerRun.StartMode.FRESH_FILL:
            # Aggregate open capacity by facility and configured period. This
            # stays proportional to facilities and rule windows, not the much
            # larger physician-by-shift cross product of a long schedule.
            initial_fill_opportunity_windows = {
                (row['window_start'], row['window_end'], row['units'])
                for rows in workload_ranges_by_physician.values()
                for row in rows if row['min_value'] is not None
            }
            for open_instance in instances:
                open_slots = max(open_instance.required_staffing - len(state[open_instance.id]), 0)
                if not open_slots:
                    continue
                for window_start, window_end, units in initial_fill_opportunity_windows:
                    if window_start <= open_instance.date <= window_end:
                        key = (window_start, window_end, units)
                        slot_units = Decimal('1') if units == 'SHIFTS' else _shift_hours(open_instance)
                        initial_fill_open_capacity[open_instance.facility_id][key] += slot_units * open_slots

        def sync_initial_fill_counters():
            initial_fill_hours.clear()
            initial_fill_shifts.clear()
            initial_fill_template_indexes.clear()
            initial_fill_intervals.clear()
            initial_fill_night_dates.clear()
            initial_fill_weekend_dates.clear()
            initial_fill_weekend_weeks.clear()
            initial_fill_assigned_dates.clear()
            for physician_id, prior_instances in published_boundary_context.items():
                for prior_instance in prior_instances:
                    initial_fill_intervals[physician_id].append((
                        prior_instance.start_datetime,
                        prior_instance.end_datetime,
                    ))
                    initial_fill_assigned_dates[physician_id].add(prior_instance.date)
                    if prior_instance.shift_template.night_shift:
                        initial_fill_night_dates[physician_id].add(prior_instance.date)
                    if _is_weekend_designated(prior_instance):
                        initial_fill_weekend_dates[physician_id].add(prior_instance.date)
                        initial_fill_weekend_weeks[physician_id].add(
                            prior_instance.date
                            - timedelta(days=prior_instance.date.weekday())
                        )
            for assigned_instance in instances:
                position = initial_fill_template_positions.get(assigned_instance.id)
                for physician_id in state[assigned_instance.id]:
                    initial_fill_intervals[physician_id].append((
                        assigned_instance.start_datetime, assigned_instance.end_datetime,
                    ))
                    initial_fill_hours[physician_id] += _shift_hours(assigned_instance)
                    initial_fill_shifts[physician_id] += 1
                    initial_fill_assigned_dates[physician_id].add(assigned_instance.date)
                    if assigned_instance.shift_template.night_shift:
                        initial_fill_night_dates[physician_id].add(assigned_instance.date)
                    if _is_weekend_designated(assigned_instance):
                        initial_fill_weekend_dates[physician_id].add(assigned_instance.date)
                        initial_fill_weekend_weeks[physician_id].add(
                            assigned_instance.date
                            - timedelta(days=assigned_instance.date.weekday())
                        )
                    if position is not None:
                        template_id, occurrence_index = position
                        initial_fill_template_indexes[(physician_id, template_id)].append(occurrence_index)

        def fill_open_instances(ordered_instances, phase):
            nonlocal assignments_made
            nonlocal rest_violations_blocked
            nonlocal night_block_assignment_attempts
            nonlocal night_block_assignment_successes
            nonlocal nonnight_assignments_blocked_by_recovery
            nonlocal nonnight_assignments_allowed_despite_recovery
            nonlocal candidates_considered_before_timeout
            nonlocal initial_fill_workload_guard_candidates_above_max
            nonlocal initial_fill_workload_guard_candidates_deprioritized

            for instance in ordered_instances:
                if runtime_exceeded():
                    mark_timeout(phase)
                    break
                while len(state[instance.id]) < instance.required_staffing:
                    if runtime_exceeded():
                        mark_timeout(phase)
                        break
                    if phase == 'night':
                        night_block_assignment_attempts += 1
                    candidates = []
                    recovery_conflict_candidates = 0
                    for physician in shuffle(physicians):
                        candidates_considered_before_timeout += 1
                        if runtime_exceeded():
                            mark_timeout(phase)
                            break
                        if physician.id in state[instance.id]:
                            continue
                        if instance.facility_id not in eligible_facilities_by_physician.get(physician.id, set()):
                            continue
                        if not _can_assign_in_state(
                            state,
                            instances_by_id,
                            instance,
                            physician.id,
                            eligible_facilities_by_physician,
                            minimum_rest_by_physician,
                            assigned_intervals=initial_fill_intervals[physician.id],
                        ):
                            rest_violations_blocked += 1
                            continue

                        contract = contract_by_physician[physician.id]
                        target = targets[physician.id]
                        shift_hours = _shift_hours(instance)
                        next_hours = initial_fill_hours[physician.id] + shift_hours
                        next_shifts = initial_fill_shifts[physician.id] + 1
                        workload_score = _workload_candidate_score(target, next_hours, next_shifts)
                        workload_rule_delta = _workload_rule_delta_from_totals(
                            workload_ranges_by_physician[physician.id],
                            workload_totals_by_physician[physician.id],
                            instance.date,
                            shift_hours,
                        )
                        workload_settings = (
                            contract.workload_settings
                            if isinstance(contract.workload_settings, dict)
                            else {}
                        )
                        max_days_in_row = _configured_positive_int(
                            workload_settings,
                            'max_days_in_row',
                        )
                        max_days_penalty = _configured_positive_penalty(
                            workload_settings,
                            'max_days_in_row_penalty_weight',
                            Decimal('0'),
                        )
                        assigned_dates = initial_fill_assigned_dates[physician.id]
                        consecutive_days_delta = Decimal('0')
                        if (
                            instance.date not in assigned_dates
                            and max_days_in_row is not None
                            and max_days_penalty > 0
                        ):
                            left_length = 0
                            prior_date = instance.date - timedelta(days=1)
                            while prior_date in assigned_dates:
                                left_length += 1
                                prior_date -= timedelta(days=1)
                            right_length = 0
                            next_date = instance.date + timedelta(days=1)
                            while next_date in assigned_dates:
                                right_length += 1
                                next_date += timedelta(days=1)
                            before_excess = (
                                max(left_length - max_days_in_row, 0)
                                + max(right_length - max_days_in_row, 0)
                            )
                            after_excess = max(
                                left_length + 1 + right_length - max_days_in_row,
                                0,
                            )
                            consecutive_days_delta = (
                                Decimal(after_excess - before_excess)
                                * max_days_penalty
                            )
                        same_shift_delta = _same_shift_candidate_delta_from_indexes(
                            contract,
                            initial_fill_template_positions,
                            initial_fill_template_indexes,
                            physician.id,
                            instance,
                        )
                        if phase == 'night':
                            night_delta = _night_volume_delta_from_totals(
                                night_ranges_by_physician[physician.id],
                                night_totals_by_physician[physician.id],
                                instance.date,
                            )
                            night_pressure = _night_volume_pressure_from_totals(
                                night_ranges_by_physician[physician.id],
                                night_totals_by_physician[physician.id],
                                instance.date,
                            )
                        else:
                            night_delta = Decimal('0')
                            night_pressure = Decimal('0')
                        if NIGHT_CONSTRUCTION_HEURISTICS_ENABLED:
                            # These two terms are inexpensive and give the
                            # constructor enough structure to form consecutive
                            # night blocks and protect their recovery days.
                            night_block_bonus = _night_block_extension_bonus(
                                instances_by_id,
                                state,
                                contract_by_physician,
                                physician.id,
                                instance,
                            )
                            recovery_penalty = _night_recovery_candidate_penalty(
                                instances,
                                physicians,
                                state,
                                contract_by_physician,
                                physician.id,
                                instance,
                            )
                            if phase == 'night':
                                settings = _night_settings(contract)
                                min_consecutive = _configured_positive_int(
                                    settings,
                                    'min_consecutive_night_shifts',
                                )
                                max_consecutive = _configured_positive_int(
                                    settings,
                                    'max_consecutive_night_shifts',
                                )
                                prior_dates = initial_fill_night_dates[physician.id]
                                previous_date = instance.date - timedelta(days=1)
                                previous_run_length = 0
                                while previous_date in prior_dates:
                                    previous_run_length += 1
                                    previous_date -= timedelta(days=1)
                                if previous_run_length:
                                    projected_run_length = previous_run_length + 1
                                    if (
                                        max_consecutive is not None
                                        and projected_run_length > max_consecutive
                                    ):
                                        night_block_priority = 3
                                        night_block_deficit = projected_run_length - max_consecutive
                                    elif (
                                        min_consecutive is not None
                                        and min_consecutive > 1
                                        and projected_run_length <= min_consecutive
                                    ):
                                        night_block_priority = 0
                                        night_block_deficit = max(
                                            min_consecutive - projected_run_length,
                                            0,
                                        )
                                    else:
                                        night_block_priority = 1
                                        night_block_deficit = 0
                                elif min_consecutive is not None and min_consecutive > 1:
                                    # Starting a block must remain neutral
                                    # across contracts.  Giving it a worse
                                    # rank than a contract with min=1 caused
                                    # the latter physicians to absorb nearly
                                    # every open night before workload limits
                                    # could participate in the comparison.
                                    night_block_priority = 1
                                    night_block_deficit = 0
                                else:
                                    night_block_priority = 1
                                    night_block_deficit = 0
                            else:
                                night_block_priority = 1
                                night_block_deficit = 0
                            # The exhaustive delta repeatedly builds a complete
                            # night report for every candidate.  Retain it only
                            # with the opt-in block builder that owns that cost.
                            if NIGHT_BLOCK_BUILDER_ENABLED:
                                night_delta += _night_candidate_delta(
                                    instances,
                                    physicians,
                                    state,
                                    contract_by_physician,
                                    physician.id,
                                    instance,
                                )
                                night_minimum_bonus = _night_minimum_candidate_bonus(
                                    instances,
                                    state,
                                    contract_by_physician,
                                    physician.id,
                                    instance,
                                )
                            else:
                                night_minimum_bonus = Decimal('0')
                        else:
                            night_block_bonus = Decimal('0')
                            night_minimum_bonus = Decimal('0')
                            recovery_penalty = Decimal('0')
                            night_block_priority = 1
                            night_block_deficit = 0
                        if _is_weekend_designated(instance):
                            weekend_settings = (
                                contract.weekend_settings
                                if isinstance(contract.weekend_settings, dict)
                                else {}
                            )
                            min_consecutive_weekend = _configured_positive_int(
                                weekend_settings,
                                'min_consecutive_weekend_shifts',
                            )
                            max_consecutive_weekend = _configured_positive_int(
                                weekend_settings,
                                'max_consecutive_weekend_shifts',
                            )
                            min_consecutive_weekends = _configured_positive_int(
                                weekend_settings,
                                'min_consecutive_weekends',
                            )
                            max_consecutive_weekends = _configured_positive_int(
                                weekend_settings,
                                'max_consecutive_weekends',
                            )
                            prior_weekend_dates = initial_fill_weekend_dates[physician.id]
                            previous_weekend_date = instance.date - timedelta(days=1)
                            previous_weekend_run_length = 0
                            while previous_weekend_date in prior_weekend_dates:
                                previous_weekend_run_length += 1
                                previous_weekend_date -= timedelta(days=1)
                            projected_weekend_run_length = (
                                previous_weekend_run_length + 1
                            )
                            weekend_start = (
                                instance.date
                                - timedelta(days=instance.date.weekday())
                            )
                            prior_weekend_weeks = initial_fill_weekend_weeks[physician.id]
                            opens_new_weekend = weekend_start not in prior_weekend_weeks
                            previous_week = weekend_start - timedelta(days=7)
                            previous_week_run_length = 0
                            while previous_week in prior_weekend_weeks:
                                previous_week_run_length += 1
                                previous_week -= timedelta(days=7)
                            projected_week_run_length = previous_week_run_length + 1
                            if (
                                previous_weekend_run_length
                                and max_consecutive_weekend is not None
                                and projected_weekend_run_length > max_consecutive_weekend
                            ) or (
                                opens_new_weekend
                                and previous_week_run_length
                                and max_consecutive_weekends is not None
                                and projected_week_run_length > max_consecutive_weekends
                            ):
                                weekend_block_priority = 3
                            elif (
                                (
                                    previous_weekend_run_length
                                    and min_consecutive_weekend is not None
                                    and min_consecutive_weekend > 1
                                    and projected_weekend_run_length <= min_consecutive_weekend
                                )
                                or (
                                    opens_new_weekend
                                    and previous_week_run_length
                                    and min_consecutive_weekends is not None
                                    and min_consecutive_weekends > 1
                                    and projected_week_run_length <= min_consecutive_weekends
                                )
                            ):
                                weekend_block_priority = 0
                            else:
                                # New blocks and already-complete blocks stay
                                # neutral so weekend construction does not
                                # overpower workload or other contract rules.
                                weekend_block_priority = 1
                        else:
                            weekend_block_priority = 1
                        if recovery_penalty > 0:
                            recovery_conflict_candidates += 1

                        matching_requests = _requests_for_shift(
                            requests_by_physician_date,
                            physician.id,
                            instance,
                        )
                        request_score = _request_candidate_rank(
                            matching_requests,
                            contract,
                        )
                        workload_rank, workload_debug = _initial_fill_workload_guard(
                            workload_ranges_by_physician[physician.id],
                            {
                                'date': instance.date,
                                'values': workload_totals_by_physician[physician.id],
                            },
                            shift_hours,
                        )
                        workload_scarcity = (
                            _initial_fill_workload_scarcity(
                                workload_ranges_by_physician[physician.id],
                                workload_totals_by_physician[physician.id],
                                {
                                    index: sum(
                                        initial_fill_open_capacity[facility_id][(
                                            row['window_start'], row['window_end'], row['units'],
                                        )]
                                        for facility_id in eligible_facilities_by_physician[physician.id]
                                    )
                                    for index, row in enumerate(workload_ranges_by_physician[physician.id])
                                    if row['min_value'] is not None
                                    and row['window_start'] <= instance.date <= row['window_end']
                                },
                                instance.date,
                            )
                            if start_mode == OptimizerRun.StartMode.FRESH_FILL
                            and workload_rank == 0
                            else Decimal('0')
                        )
                        if workload_rank == 2:
                            initial_fill_workload_guard_candidates_above_max += 1
                            initial_fill_workload_guard_candidates_deprioritized += 1
                            if len(initial_fill_workload_guard_examples) < 10:
                                initial_fill_workload_guard_examples.append({
                                    'physician_id': physician.id,
                                    'physician': _physician_display_name(physician),
                                    **_contract_rule_identity(contract),
                                    **workload_debug,
                                })
                        candidates.append(
                            (
                                recovery_penalty > 0,
                                request_score,
                                consecutive_days_delta > 0,
                                consecutive_days_delta,
                                night_block_priority,
                                weekend_block_priority,
                                (
                                    workload_rank
                                    if start_mode == OptimizerRun.StartMode.FRESH_FILL
                                    else 1
                                ),
                                -workload_scarcity,
                                night_block_deficit,
                                night_delta,
                                night_pressure,
                                workload_rank,
                                workload_rule_delta,
                                workload_score
                                + same_shift_delta
                                + night_delta
                                + night_block_bonus
                                + night_minimum_bonus
                                + (recovery_penalty * RECOVERY_CONFLICT_AVOIDANCE_MULTIPLIER),
                                rng.random(),
                                physician,
                            )
                        )

                    if timed_out or not candidates:
                        break

                    clean_candidates = [
                        candidate for candidate in candidates
                        if not candidate[0]
                    ]
                    candidate_pool = clean_candidates or candidates
                    if phase == 'non_night' and recovery_conflict_candidates:
                        if clean_candidates:
                            nonnight_assignments_blocked_by_recovery += recovery_conflict_candidates
                        else:
                            nonnight_assignments_allowed_despite_recovery += 1

                    (
                        _has_recovery_conflict,
                        _request_score_value,
                        _has_consecutive_days_penalty,
                        _consecutive_days_delta,
                        _night_block_priority,
                        _weekend_block_priority,
                        _fresh_fill_workload_rank,
                        _workload_scarcity,
                        _night_block_deficit,
                        _night_delta_value,
                        _night_pressure_value,
                        _workload_rank,
                        _workload_rule_delta,
                        _candidate_score,
                        _tie_breaker,
                        selected_physician,
                    ) = min(candidate_pool)
                    _add_to_state(state, instance.id, selected_physician.id)
                    if initial_fill_opportunity_windows:
                        for window_start, window_end, units in initial_fill_opportunity_windows:
                            if window_start <= instance.date <= window_end:
                                key = (window_start, window_end, units)
                                slot_units = Decimal('1') if units == 'SHIFTS' else _shift_hours(instance)
                                initial_fill_open_capacity[instance.facility_id][key] -= slot_units
                    initial_fill_intervals[selected_physician.id].append((
                        instance.start_datetime, instance.end_datetime,
                    ))
                    initial_fill_hours[selected_physician.id] += _shift_hours(instance)
                    initial_fill_shifts[selected_physician.id] += 1
                    initial_fill_assigned_dates[selected_physician.id].add(instance.date)
                    selected_position = initial_fill_template_positions.get(instance.id)
                    if selected_position is not None:
                        template_id, occurrence_index = selected_position
                        initial_fill_template_indexes[(selected_physician.id, template_id)].append(
                            occurrence_index
                        )
                    for row in workload_ranges_by_physician[selected_physician.id]:
                        if row['window_start'] <= instance.date <= row['window_end']:
                            key = (row['window_start'], row['window_end'], row['units'])
                            workload_totals_by_physician[selected_physician.id][key] += (
                                Decimal('1') if row['units'] == 'SHIFTS' else _shift_hours(instance)
                            )
                    if phase == 'night':
                        initial_fill_night_dates[selected_physician.id].add(instance.date)
                        for row in night_ranges_by_physician[selected_physician.id]:
                            if row['window_start'] <= instance.date <= row['window_end']:
                                key = (row['window_start'], row['window_end'])
                                night_totals_by_physician[selected_physician.id][key] += Decimal('1')
                    if _is_weekend_designated(instance):
                        initial_fill_weekend_dates[selected_physician.id].add(instance.date)
                        initial_fill_weekend_weeks[selected_physician.id].add(
                            instance.date - timedelta(days=instance.date.weekday())
                        )
                    assignments_made += 1
                    if phase == 'night':
                        night_block_assignment_successes += 1

        if NIGHT_BLOCK_BUILDER_ENABLED:
            build_night_blocks()
        sync_initial_fill_counters()
        # Chronological construction is essential for the extension bonus to
        # see yesterday's assignment when choosing today's physician.  Keep
        # random tie-breaking among simultaneous slots so fresh fills remain
        # meaningfully diversified across seeds.
        night_instances = random_tie_sorted(
            night_instances,
            key=lambda item: (item.date, item.start_datetime),
        )
        # Weekend-block construction also needs yesterday's assignment to be
        # visible when evaluating today. Randomize only simultaneous slots;
        # shuffling the entire phase destroys that local structure.
        non_night_instances = random_tie_sorted(
            non_night_instances,
            key=lambda item: (item.date, item.start_datetime),
        )
        if not timed_out:
            fill_open_instances(night_instances, 'night')
        if not timed_out:
            fill_open_instances(non_night_instances, 'non_night')

        assignment_pairs_at_first_score = {
            (instance_id, physician_id)
            for instance_id, physician_ids in state.items()
            for physician_id in physician_ids
        }
        assignment_pairs_added_before_first_score = (
            assignment_pairs_at_first_score - assignment_pairs_before_pre_score_changes
        )
        assignment_pairs_removed_before_first_score = (
            assignment_pairs_before_pre_score_changes - assignment_pairs_at_first_score
        )

        initial_scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
            include_internal_night_heuristics=True,
        )
        search_initial_score = initial_scoring['score']
        reported_initial_score = (
            (
                float(source_run.final_score)
                if source_run.final_score is not None
                else source_state_reported_score_before_pre_score_changes
            )
            if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
            and source_run is not None
            else search_initial_score
        )
        same_shift_violations_initial = len(initial_scoring['same_shift_violations'])
        initial_night_report = _night_violation_report(
            instances,
            physicians,
            state,
            contract_by_physician,
        )
        initial_night_block_debug = _night_block_debug(
            instances,
            physicians,
            state,
            contract_by_physician,
        )
        initial_workload_range_counts = _workload_range_counts(initial_scoring)
        final_score = search_initial_score
        progress_ready = True
        improvement_count = 0
        iterations_run = 0
        max_passes = (
            MAX_PHASE_PASSES
            if NIGHT_BLOCK_BUILDER_ENABLED
            else SAFE_BASELINE_PHASE_PASSES
        )
        max_candidates_per_repair = (
            MAX_CANDIDATES_PER_REPAIR
            if NIGHT_BLOCK_BUILDER_ENABLED
            else SAFE_BASELINE_CANDIDATES_PER_REPAIR
        )
        max_swap_attempts_per_pass = (
            MAX_GENERAL_SWAPS
            if NIGHT_BLOCK_BUILDER_ENABLED
            else SAFE_BASELINE_GENERAL_SWAPS
        )
        physician_ids = [physician.id for physician in physicians]
        phase_order = [
            'request_on_preflight',
            'workload_revisit',
            'night_spacing_revisit',
            'request_repair',
            'night_minimum_repair',
            'post_night_recovery_repair',
            'workload_range_repair',
            'workload_minimum_repair',
            'same_shift_repair',
            'consecutive_day_repair',
            'general_hill_climb_swaps',
            'final_plateau_repair',
        ]
        phase_attempts = defaultdict(int)
        phase_runtime_seconds = defaultdict(float)
        phase_improvements = defaultdict(int)
        phase_passes_run = 0
        stopped_reason = 'no_improvement'
        request_repair_attempts = 0
        request_repair_improvements = 0
        post_night_recovery_repair_attempts = 0
        post_night_recovery_repair_improvements = 0
        workload_repair_attempts = 0
        workload_repair_improvements = 0
        workload_candidate_moves_considered = 0
        reassignment_candidates_rejected_incrementally = 0
        reassignment_candidates_full_score_verified = 0
        workload_candidate_swaps_considered = 0
        workload_moves_accepted = 0
        workload_swaps_accepted = 0
        workload_minimum_repair_physicians_considered = 0
        workload_minimum_repair_candidates_considered = 0
        workload_minimum_repair_accepts = 0
        workload_minimum_repair_best_delta = None
        workload_minimum_repair_rejected_reasons = defaultdict(int)
        workload_minimum_repair_turner_summary = {
            'physicians': [],
            'candidates_considered': 0,
            'accepts': [],
            'rejected_reasons': defaultdict(int),
        }
        general_swap_attempts = 0
        general_swap_improvements = 0
        workload_transfer_attempts = 0
        workload_transfer_improvements = 0
        same_shift_break_attempts = 0
        same_shift_break_improvements = 0
        same_shift_fix_valid_alternatives = 0
        night_fix_attempts = 0
        night_fix_valid_alternatives = 0
        night_fix_improvements = 0
        night_minimum_fix_attempts = 0
        night_minimum_fix_valid_alternatives = 0
        night_minimum_fix_improvements = 0
        night_minimum_direct_reassignment_attempts = 0
        night_minimum_direct_reassignment_successes = 0
        night_minimum_swap_attempts = 0
        night_minimum_swap_successes = 0
        night_minimum_repair_debug = []
        consecutive_day_break_attempts = 0
        consecutive_day_break_improvements = 0
        swap_attempts = 0
        swap_improvements = 0
        final_plateau_repair_attempts = 0
        final_plateau_repair_accepts = 0
        workload_micro_repairs_attempted = 0
        workload_micro_repairs_accepted = 0
        workload_micro_repair_best_delta = None
        workload_micro_repair_best_rejected_reason = None
        workload_micro_repair_accepted_details = []
        over_max_physicians_considered = []
        night_recovery_repairs_attempted = 0
        night_recovery_repairs_accepted = 0
        pairwise_swaps_attempted = 0
        pairwise_swaps_accepted = 0
        best_pairwise_swap_delta = None
        accepted_pairwise_swap_details = []
        pairwise_rescan_rounds = 0
        pairwise_candidates_considered = 0
        pairwise_candidates_skipped_by_cap = 0
        pairwise_best_missed_candidate_if_any = None
        pairwise_accepts_after_rescan = 0
        weekend_repairs_attempted = 0
        weekend_repairs_accepted = 0
        weekend_repair_rescan_rounds = 0
        score_before_final_plateau_repair = None
        score_after_final_plateau_repair = None
        final_plateau_repair_reason = 'not_run'

        def score_is_zero():
            current = _score_schedule(
                instances,
                physicians,
                state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            return all(
                value <= Decimal('0')
                for key, value in current['breakdown'].items()
                if key != 'total_score'
            )

        def is_optimization_improvement(candidate_scoring):
            current_priority = (
                _fixed_request_on_unmet_count(
                    instances, state, requests_by_physician_date,
                ),
                final_score,
            )
            return _optimization_priority(candidate_scoring) < current_priority

        def try_reassign(instance_id, from_physician_id, to_physician_id):
            if to_physician_id == from_physician_id:
                return None
            if to_physician_id in state[instance_id]:
                return None
            instance = instances_by_id[instance_id]
            trial_state = _copy_state(state)
            _replace_in_state(
                trial_state,
                instance_id,
                from_physician_id,
                to_physician_id,
            )
            if not _can_assign_in_state(
                trial_state,
                instances_by_id,
                instance,
                to_physician_id,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                exclude_instance_id=instance_id,
            ):
                return None
            return _score_schedule(
                instances,
                physicians,
                trial_state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            ), trial_state

        def has_hard_invalids(scoring):
            validation = scoring['validation']
            return any(
                validation.get(key, 0)
                for key in (
                    'final_overlap_violations',
                    'final_rest_violations',
                    'final_duplicate_violations',
                    'final_overstaffed_violations',
                    'final_inactive_physician_violations',
                    'final_facility_ineligible_violations',
                )
            )

        def try_swap(left_instance_id, left_physician_id, right_instance_id, right_physician_id):
            if left_instance_id == right_instance_id:
                return None
            if left_physician_id == right_physician_id:
                return None
            if right_physician_id in state[left_instance_id]:
                return None
            if left_physician_id in state[right_instance_id]:
                return None
            left_instance = instances_by_id[left_instance_id]
            right_instance = instances_by_id[right_instance_id]
            trial_state = _copy_state(state)
            _replace_in_state(
                trial_state,
                left_instance_id,
                left_physician_id,
                right_physician_id,
            )
            _replace_in_state(
                trial_state,
                right_instance_id,
                right_physician_id,
                left_physician_id,
            )
            if not _can_assign_in_state(
                trial_state,
                instances_by_id,
                left_instance,
                right_physician_id,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                exclude_instance_id=left_instance_id,
            ):
                return None
            if not _can_assign_in_state(
                trial_state,
                instances_by_id,
                right_instance,
                left_physician_id,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                exclude_instance_id=right_instance_id,
            ):
                return None
            return _score_schedule(
                instances,
                physicians,
                trial_state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            ), trial_state

        def try_three_cycle(
            requested_instance_id,
            current_owner_id,
            requested_physician_id,
            requester_instance_id,
            third_instance_id,
            third_physician_id,
        ):
            if len({current_owner_id, requested_physician_id, third_physician_id}) != 3:
                return None
            if len({requested_instance_id, requester_instance_id, third_instance_id}) != 3:
                return None
            requested_instance = instances_by_id[requested_instance_id]
            requester_instance = instances_by_id[requester_instance_id]
            third_instance = instances_by_id[third_instance_id]
            trial_state = _copy_state(state)
            _replace_in_state(
                trial_state, requested_instance_id,
                current_owner_id, requested_physician_id,
            )
            _replace_in_state(
                trial_state, requester_instance_id,
                requested_physician_id, third_physician_id,
            )
            _replace_in_state(
                trial_state, third_instance_id,
                third_physician_id, current_owner_id,
            )
            checks = (
                (requested_instance, requested_physician_id, requested_instance_id),
                (requester_instance, third_physician_id, requester_instance_id),
                (third_instance, current_owner_id, third_instance_id),
            )
            if any(
                not _can_assign_in_state(
                    trial_state,
                    instances_by_id,
                    candidate_instance,
                    candidate_physician_id,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    exclude_instance_id=exclude_instance_id,
                )
                for candidate_instance, candidate_physician_id, exclude_instance_id in checks
            ):
                return None
            return _score_schedule(
                instances,
                physicians,
                trial_state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            ), trial_state

        def try_request_centered_rebuild(
            requested_instance_id,
            current_owner_id,
            requested_physician_id,
            *,
            max_depth=5,
            node_budget=3000,
        ):
            """Find a bounded assignment cycle that satisfies a request-on."""
            requested_instance = instances_by_id[requested_instance_id]
            initial_state = _copy_state(state)
            _replace_in_state(
                initial_state,
                requested_instance_id,
                current_owner_id,
                requested_physician_id,
            )
            nodes = 0

            def ordered_outgoing(trial_state, giver_id, used_instance_ids):
                candidate_ids = [
                    candidate_id
                    for candidate_id, physician_ids in trial_state.items()
                    if giver_id in physician_ids
                    and candidate_id not in used_instance_ids
                    and (candidate_id, giver_id) not in manual_pairs
                ]
                candidate_ids.sort(
                    key=lambda candidate_id: (
                        instances_by_id[candidate_id].shift_template.night_shift
                        != requested_instance.shift_template.night_shift,
                        abs(
                            _shift_hours(instances_by_id[candidate_id])
                            - _shift_hours(requested_instance)
                        ),
                        abs(
                            (instances_by_id[candidate_id].date - requested_instance.date).days
                        ),
                        candidate_id,
                    )
                )
                return candidate_ids[:16]

            def completed_result(trial_state):
                scoring = _score_schedule(
                    instances,
                    physicians,
                    trial_state,
                    targets,
                    contract_by_physician,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    include_internal_night_heuristics=True,
                )
                if has_hard_invalids(scoring) or not is_optimization_improvement(scoring):
                    return None
                return scoring, trial_state

            def search(trial_state, giver_id, used_physician_ids, used_instance_ids, depth):
                nonlocal nodes
                if nodes >= node_budget:
                    return None
                for outgoing_id in ordered_outgoing(trial_state, giver_id, used_instance_ids):
                    if nodes >= node_budget:
                        break
                    outgoing = instances_by_id[outgoing_id]

                    # Closing the cycle restores the original owner's workload.
                    if current_owner_id not in trial_state[outgoing_id]:
                        nodes += 1
                        closed_state = _copy_state(trial_state)
                        _replace_in_state(
                            closed_state,
                            outgoing_id,
                            giver_id,
                            current_owner_id,
                        )
                        result = completed_result(closed_state)
                        if result is not None:
                            return result

                    if depth >= max_depth:
                        continue
                    recipient_ids = [
                        physician.id
                        for physician in physicians
                        if physician.id not in used_physician_ids
                        and physician.id != current_owner_id
                        and outgoing.facility_id
                        in eligible_facilities_by_physician.get(physician.id, set())
                        and physician.id not in trial_state[outgoing_id]
                    ]
                    recipient_ids.sort(
                        key=lambda physician_id: (
                            abs(
                                sum(
                                    _shift_hours(instances_by_id[candidate_id])
                                    for candidate_id, physician_ids in trial_state.items()
                                    if physician_id in physician_ids
                                )
                                - sum(
                                    _shift_hours(instances_by_id[candidate_id])
                                    for candidate_id, physician_ids in trial_state.items()
                                    if giver_id in physician_ids
                                )
                            ),
                            physician_id,
                        )
                    )
                    for recipient_id in recipient_ids[:12]:
                        if nodes >= node_budget:
                            break
                        nodes += 1
                        next_state = _copy_state(trial_state)
                        _replace_in_state(
                            next_state,
                            outgoing_id,
                            giver_id,
                            recipient_id,
                        )
                        result = search(
                            next_state,
                            recipient_id,
                            {*used_physician_ids, recipient_id},
                            {*used_instance_ids, outgoing_id},
                            depth + 1,
                        )
                        if result is not None:
                            return result
                return None

            result = search(
                initial_state,
                requested_physician_id,
                {requested_physician_id},
                {requested_instance_id},
                1,
            )
            return result, nodes

        def repair_requested_shifts_on(candidate_budget):
            """Exhaust improving directed request-on moves before general search."""
            nonlocal state, final_score, improvement_count
            nonlocal request_repair_attempts, request_repair_improvements
            nonlocal candidates_considered_before_timeout, iterations_run

            accepted = 0
            attempts = 0
            while attempts < candidate_budget and runtime_seconds_elapsed() < runtime_limit_seconds:
                directed_candidates = _request_on_repair_candidates(
                    instances,
                    state,
                    manual_pairs,
                    contract_by_physician,
                    requests_by_physician_date,
                )
                if not directed_candidates:
                    break
                improved_request = False
                for requested_physician_id, from_physician_id, instance_id in directed_candidates:
                    if attempts >= candidate_budget or runtime_seconds_elapsed() >= runtime_limit_seconds:
                        break
                    attempts += 1
                    candidates_considered_before_timeout += 1
                    iterations_run += 1
                    request_repair_attempts += 1
                    phase_attempts['request_on_preflight'] += 1
                    result = try_reassign(
                        instance_id,
                        from_physician_id,
                        requested_physician_id,
                    )
                    if result is not None and is_optimization_improvement(result[0]):
                        final_score, state = result[0]['score'], result[1]
                        improvement_count += 1
                        request_repair_improvements += 1
                        phase_improvements['request_on_preflight'] += 1
                        accepted += 1
                        improved_request = True
                        break

                    requested_instance = instances_by_id[instance_id]
                    owned_swap_candidates = [
                        owned_instance_id
                        for owned_instance_id, physician_ids in state.items()
                        if requested_physician_id in physician_ids
                        and owned_instance_id != instance_id
                        and (owned_instance_id, requested_physician_id) not in manual_pairs
                    ]
                    owned_swap_candidates.sort(
                        key=lambda owned_instance_id: (
                            instances_by_id[owned_instance_id].date != requested_instance.date,
                            instances_by_id[owned_instance_id].shift_template.night_shift
                            != requested_instance.shift_template.night_shift,
                            abs(
                                _shift_hours(instances_by_id[owned_instance_id])
                                - _shift_hours(requested_instance)
                            ),
                            abs(
                                (
                                    instances_by_id[owned_instance_id].date
                                    - requested_instance.date
                                ).days
                            ),
                            owned_instance_id,
                        )
                    )
                    for owned_instance_id in owned_swap_candidates:
                        if attempts >= candidate_budget:
                            break
                        attempts += 1
                        candidates_considered_before_timeout += 1
                        iterations_run += 1
                        request_repair_attempts += 1
                        phase_attempts['request_on_preflight'] += 1
                        result = try_swap(
                            instance_id,
                            from_physician_id,
                            owned_instance_id,
                            requested_physician_id,
                        )
                        if result is None or not is_optimization_improvement(result[0]):
                            continue
                        final_score, state = result[0]['score'], result[1]
                        improvement_count += 1
                        request_repair_improvements += 1
                        phase_improvements['request_on_preflight'] += 1
                        accepted += 1
                        improved_request = True
                        break
                    if improved_request:
                        break

                    remaining_budget = candidate_budget - attempts
                    if remaining_budget > 0:
                        result, rebuild_nodes = try_request_centered_rebuild(
                            instance_id,
                            from_physician_id,
                            requested_physician_id,
                            node_budget=min(3000, remaining_budget),
                        )
                        attempts += rebuild_nodes
                        candidates_considered_before_timeout += rebuild_nodes
                        iterations_run += rebuild_nodes
                        request_repair_attempts += rebuild_nodes
                        phase_attempts['request_on_preflight'] += rebuild_nodes
                        if result is not None:
                            final_score, state = result[0]['score'], result[1]
                            improvement_count += 1
                            request_repair_improvements += 1
                            phase_improvements['request_on_preflight'] += 1
                            accepted += 1
                            improved_request = True
                            break

                    # If a two-person exchange cannot preserve both workloads,
                    # try a bounded three-person rotation with comparable shifts.
                    for requester_instance_id in owned_swap_candidates:
                        if attempts >= candidate_budget:
                            break
                        requester_instance = instances_by_id[requester_instance_id]
                        third_pairs = [
                            (third_instance_id, third_physician_id)
                            for third_instance_id, physician_ids in state.items()
                            for third_physician_id in physician_ids
                            if third_physician_id not in {
                                from_physician_id,
                                requested_physician_id,
                            }
                            and third_instance_id != requester_instance_id
                            and (third_instance_id, third_physician_id) not in manual_pairs
                            and instances_by_id[third_instance_id].shift_template.night_shift
                            == requested_instance.shift_template.night_shift
                        ]
                        third_pairs.sort(
                            key=lambda pair: (
                                abs(
                                    _shift_hours(instances_by_id[pair[0]])
                                    - _shift_hours(requested_instance)
                                ),
                                abs(
                                    (instances_by_id[pair[0]].date - requested_instance.date).days
                                ),
                                pair[0],
                                pair[1],
                            )
                        )
                        for third_instance_id, third_physician_id in third_pairs:
                            if attempts >= candidate_budget:
                                break
                            attempts += 1
                            candidates_considered_before_timeout += 1
                            iterations_run += 1
                            request_repair_attempts += 1
                            phase_attempts['request_on_preflight'] += 1
                            result = try_three_cycle(
                                instance_id,
                                from_physician_id,
                                requested_physician_id,
                                requester_instance_id,
                                third_instance_id,
                                third_physician_id,
                            )
                            if result is None or not is_optimization_improvement(result[0]):
                                continue
                            final_score, state = result[0]['score'], result[1]
                            improvement_count += 1
                            request_repair_improvements += 1
                            phase_improvements['request_on_preflight'] += 1
                            accepted += 1
                            improved_request = True
                            break
                        if improved_request:
                            break
                    if improved_request:
                        break
                if not improved_request:
                    break
            return accepted

        def repair_requested_shift_blocks(max_neighborhoods=4):
            """Repair coupled request-on blocks before single-move search."""
            nonlocal state, final_score, improvement_count
            nonlocal request_repair_attempts, request_repair_improvements
            nonlocal candidates_considered_before_timeout, iterations_run

            accepted = 0
            attempted_cluster_keys = set()
            if not instances:
                return accepted
            schedule_start = min(instance.date for instance in instances)
            schedule_end = max(instance.date for instance in instances)
            boundary_padding = _constraint_boundary_padding_days(
                contract_by_physician,
            )

            while accepted < max_neighborhoods and not runtime_exceeded():
                directed = _request_on_repair_candidates(
                    instances,
                    state,
                    manual_pairs,
                    contract_by_physician,
                    requests_by_physician_date,
                )
                if not directed:
                    break

                instances_by_requester = defaultdict(list)
                for requested_physician_id, _owner_id, instance_id in directed:
                    requested_instance = instances_by_id[instance_id]
                    matching_requests = [
                        request
                        for request in requests_by_physician_date.get(
                            (requested_physician_id, requested_instance.date), ()
                        )
                        if request.request_type in {
                            ScheduleRequest.RequestType.DAY_ON,
                            ScheduleRequest.RequestType.SHIFT_ON,
                        }
                        and (
                            request.request_type == ScheduleRequest.RequestType.DAY_ON
                            or requested_instance.shift_template_id in {
                                template.id for template in request.shift_templates.all()
                            }
                        )
                    ]
                    is_fixed = any(
                        request.weight == ScheduleRequest.Weight.FIXED
                        for request in matching_requests
                    )
                    instances_by_requester[
                        requested_physician_id, is_fixed
                    ].append(requested_instance)

                clusters = []
                for (physician_id, is_fixed), requested_instances in instances_by_requester.items():
                    ordered = sorted(
                        {item.id: item for item in requested_instances}.values(),
                        key=lambda item: (item.date, item.id),
                    )
                    current = []
                    for requested_instance in ordered:
                        if (
                            current
                            and requested_instance.date
                            > current[-1].date + timedelta(days=1)
                        ):
                            clusters.append((physician_id, is_fixed, current))
                            current = []
                        current.append(requested_instance)
                    if current:
                        clusters.append((physician_id, is_fixed, current))

                def cluster_penalty(item):
                    physician_id, _is_fixed, requested_instances = item
                    contract = contract_by_physician[physician_id]
                    return sum(
                        max(
                            (
                                _request_weight(contract, request.weight)
                                for request in requests_by_physician_date.get(
                                    (physician_id, requested_instance.date), ()
                                )
                                if request.request_type in {
                                    ScheduleRequest.RequestType.DAY_ON,
                                    ScheduleRequest.RequestType.SHIFT_ON,
                                }
                            ),
                            default=Decimal('0'),
                        )
                        for requested_instance in requested_instances
                    )

                clusters.sort(key=lambda item: (
                    not item[1],
                    -cluster_penalty(item),
                    item[2][0].date,
                    item[0],
                ))

                improved_cluster = False
                for physician_id, is_fixed, cluster in clusters:
                    cluster_key = (
                        physician_id,
                        tuple(instance.id for instance in cluster),
                    )
                    if cluster_key in attempted_cluster_keys:
                        continue
                    attempted_cluster_keys.add(cluster_key)
                    cluster_ids = {instance.id for instance in cluster}
                    focus_start = max(
                        schedule_start,
                        cluster[0].date - timedelta(days=boundary_padding),
                    )
                    focus_end = min(
                        schedule_end,
                        cluster[-1].date + timedelta(days=boundary_padding),
                    )
                    current_scoring = _score_schedule(
                        instances, physicians, state, targets,
                        contract_by_physician, requests_by_physician_date,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        include_internal_night_heuristics=True,
                    )

                    # First try the common corrected-request case directly:
                    # exchange every requested shift in the compact block with
                    # the requester's existing shift on that date. Evaluating
                    # the whole block at once avoids transient rest conflicts
                    # between the old day series and the new night series.
                    direct_state = _copy_state(state)
                    used_outgoing_ids = set()
                    direct_possible = True
                    for requested_instance in cluster:
                        current_owners = [
                            owner_id for owner_id in direct_state[requested_instance.id]
                            if owner_id != physician_id
                            and (requested_instance.id, owner_id) not in manual_pairs
                        ]
                        if not current_owners:
                            direct_possible = False
                            break
                        current_owner_id = current_owners[0]
                        outgoing_candidates = [
                            candidate
                            for candidate in instances
                            if candidate.date == requested_instance.date
                            and candidate.id != requested_instance.id
                            and candidate.id not in used_outgoing_ids
                            and physician_id in direct_state[candidate.id]
                            and (candidate.id, physician_id) not in manual_pairs
                            and current_owner_id not in direct_state[candidate.id]
                        ]
                        outgoing_candidates.sort(key=lambda candidate: (
                            candidate.facility_id != requested_instance.facility_id,
                            abs(
                                _shift_hours(candidate)
                                - _shift_hours(requested_instance)
                            ),
                            candidate.id,
                        ))
                        _replace_in_state(
                            direct_state,
                            requested_instance.id,
                            current_owner_id,
                            physician_id,
                        )
                        if outgoing_candidates:
                            outgoing = outgoing_candidates[0]
                            used_outgoing_ids.add(outgoing.id)
                            _replace_in_state(
                                direct_state,
                                outgoing.id,
                                physician_id,
                                current_owner_id,
                            )

                    if direct_possible:
                        direct_scoring = _score_schedule(
                            instances, physicians, direct_state, targets,
                            contract_by_physician, requests_by_physician_date,
                            eligible_facilities_by_physician,
                            minimum_rest_by_physician,
                            include_internal_night_heuristics=True,
                        )
                        request_repair_attempts += 1
                        phase_attempts['request_on_preflight'] += 1
                        candidates_considered_before_timeout += 1
                        iterations_run += 1
                        if (
                            not has_hard_invalids(direct_scoring)
                            and is_optimization_improvement(direct_scoring)
                        ):
                            state = direct_state
                            final_score = direct_scoring['score']
                            improvement_count += 1
                            request_repair_improvements += 1
                            phase_improvements['request_on_preflight'] += 1
                            accepted += 1
                            improved_cluster = True
                            break

                    if not is_fixed:
                        continue

                    request_padding = min(boundary_padding, 1)
                    focus_start = max(
                        schedule_start,
                        cluster[0].date - timedelta(days=request_padding),
                    )
                    focus_end = min(
                        schedule_end,
                        cluster[-1].date + timedelta(days=request_padding),
                    )
                    trial_state, trial_scoring, neighborhood_debug = (
                        _solve_bounded_multi_physician_neighborhood(
                            instances=instances,
                            physicians=physicians,
                            state=state,
                            scoring=current_scoring,
                            manual_pairs=manual_pairs,
                            targets=targets,
                            contract_by_physician=contract_by_physician,
                            requests_by_physician_date=requests_by_physician_date,
                            eligible_facilities_by_physician=(
                                eligible_facilities_by_physician
                            ),
                            minimum_rest_by_physician=minimum_rest_by_physician,
                            rng=rng,
                            focus_physician_ids={physician_id},
                            focus_instance_ids=cluster_ids,
                            focus_start=focus_start,
                            focus_end=focus_end,
                            cohort_size=min(max(len(cluster_ids) + 3, 8), 16),
                            time_limit_seconds=4,
                            should_stop=lambda: search_budget.reason() is not None,
                        )
                    )
                    request_repair_attempts += 1
                    phase_attempts['request_on_preflight'] += 1
                    candidates_considered_before_timeout += 1
                    iterations_run += 1
                    if not neighborhood_debug.get('accepted'):
                        continue
                    if not is_optimization_improvement(trial_scoring):
                        continue
                    state = trial_state
                    final_score = trial_scoring['score']
                    improvement_count += 1
                    request_repair_improvements += 1
                    phase_improvements['request_on_preflight'] += 1
                    accepted += 1
                    improved_cluster = True
                    break
                if not improved_cluster:
                    break
            return accepted

        request_preflight_started = runtime_seconds_elapsed()
        repair_requested_shift_blocks()
        repair_requested_shifts_on(max(5000, max_candidates_per_repair * max(max_passes, 1)))
        phase_runtime_seconds['request_on_preflight'] += (
            runtime_seconds_elapsed() - request_preflight_started
        )

        minimum_status = (
            _night_minimum_status(
                instances,
                physicians,
                state,
                contract_by_physician,
            )
            if NIGHT_BLOCK_BUILDER_ENABLED
            else {
                'physicians_under_night_minimum': [],
                'physicians_over_night_minimum': [],
            }
        )
        under_minimum_ids = {
            row['physician_id']
            for row in minimum_status['physicians_under_night_minimum']
        }
        over_minimum_ids = {
            row['physician_id']
            for row in minimum_status['physicians_over_night_minimum']
        }
        for to_physician_id in shuffle(sorted(under_minimum_ids)):
            if runtime_exceeded():
                mark_timeout('night_minimum_pre_repair')
                break
            improved_minimum = False
            night_sources = [
                (instance_id, from_physician_id)
                for instance_id, from_physician_id in _optimizer_pairs(state, manual_pairs)
                if from_physician_id in over_minimum_ids
                and instances_by_id[instance_id].shift_template.night_shift
            ]
            night_sources = shuffle(night_sources)
            for instance_id, from_physician_id in night_sources:
                candidates_considered_before_timeout += 1
                if runtime_exceeded():
                    mark_timeout('night_minimum_pre_repair')
                    break
                night_minimum_fix_attempts += 1
                result = try_reassign(instance_id, from_physician_id, to_physician_id)
                if result is None:
                    continue
                night_minimum_fix_valid_alternatives += 1
                trial_scoring, trial_state = result
                if is_optimization_improvement(trial_scoring):
                    state = trial_state
                    final_score = trial_scoring['score']
                    improvement_count += 1
                    night_minimum_fix_improvements += 1
                    improved_minimum = True
                    minimum_status = _night_minimum_status(
                        instances,
                        physicians,
                        state,
                        contract_by_physician,
                    )
                    under_minimum_ids = {
                        row['physician_id']
                        for row in minimum_status['physicians_under_night_minimum']
                    }
                    over_minimum_ids = {
                        row['physician_id']
                        for row in minimum_status['physicians_over_night_minimum']
                    }
                    break
            if improved_minimum:
                continue

            non_night_pairs = [
                (instance_id, physician_id)
                for instance_id, physician_id in _optimizer_pairs(state, manual_pairs)
                if physician_id == to_physician_id
                and not instances_by_id[instance_id].shift_template.night_shift
            ]
            donor_night_pairs = [
                (instance_id, physician_id)
                for instance_id, physician_id in _optimizer_pairs(state, manual_pairs)
                if physician_id in over_minimum_ids
                and instances_by_id[instance_id].shift_template.night_shift
            ]
            non_night_pairs = shuffle(non_night_pairs)
            donor_night_pairs = shuffle(donor_night_pairs)
            for non_night_instance_id, under_physician_id in non_night_pairs:
                if runtime_exceeded():
                    mark_timeout('night_minimum_pre_repair')
                    break
                if improved_minimum:
                    break
                for night_instance_id, donor_physician_id in donor_night_pairs:
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('night_minimum_pre_repair')
                        break
                    night_minimum_fix_attempts += 1
                    trial_state = _copy_state(state)
                    _replace_in_state(
                        trial_state,
                        night_instance_id,
                        donor_physician_id,
                        under_physician_id,
                    )
                    _replace_in_state(
                        trial_state,
                        non_night_instance_id,
                        under_physician_id,
                        donor_physician_id,
                    )
                    night_instance = instances_by_id[night_instance_id]
                    non_night_instance = instances_by_id[non_night_instance_id]
                    if not _can_assign_in_state(
                        trial_state,
                        instances_by_id,
                        night_instance,
                        under_physician_id,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        exclude_instance_id=night_instance_id,
                    ):
                        continue
                    if not _can_assign_in_state(
                        trial_state,
                        instances_by_id,
                        non_night_instance,
                        donor_physician_id,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        exclude_instance_id=non_night_instance_id,
                    ):
                        continue
                    night_minimum_fix_valid_alternatives += 1
                    trial_scoring = _score_schedule(
                        instances,
                        physicians,
                        trial_state,
                        targets,
                        contract_by_physician,
                        requests_by_physician_date,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        include_internal_night_heuristics=True,
                    )
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        night_minimum_fix_improvements += 1
                        improved_minimum = True
                        minimum_status = _night_minimum_status(
                            instances,
                            physicians,
                            state,
                            contract_by_physician,
                        )
                        under_minimum_ids = {
                            row['physician_id']
                            for row in minimum_status['physicians_under_night_minimum']
                        }
                        over_minimum_ids = {
                            row['physician_id']
                            for row in minimum_status['physicians_over_night_minimum']
                        }
                        break

        workload_revisit_debug = {'attempts': 0, 'accepts': [], 'stopped_reason': 'not_run'}
        if max_passes > 0 and not runtime_exceeded() and start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE:
            # Reserve at least half the configured budget for the other repair phases.
            workload_deadline = min(runtime_limit_seconds / 2, runtime_seconds_elapsed() + 20)
            state, _workload_scoring, workload_revisit_debug = _run_productive_repair_rounds(
                _repair_workload_transfers, elapsed=runtime_seconds_elapsed,
                deadline=workload_deadline, candidate_limit=2000,
                on_improvement=observe_best if adaptive_runtime else None,
                stop_requested=search_budget.reason if adaptive_runtime else None,
                instances=instances, physicians=physicians, state=state,
                manual_pairs=manual_pairs, targets=targets,
                contract_by_physician=contract_by_physician,
                requests_by_physician_date=requests_by_physician_date,
                eligible_facilities_by_physician=eligible_facilities_by_physician,
                minimum_rest_by_physician=minimum_rest_by_physician,
            )
            improvement_count += len(workload_revisit_debug['accepts'])
            workload_moves_accepted += len(workload_revisit_debug['accepts'])
            workload_candidate_moves_considered += workload_revisit_debug['attempts']
            candidates_considered_before_timeout += workload_revisit_debug['attempts']
            phase_attempts['workload_revisit'] += workload_revisit_debug['attempts']
            phase_improvements['workload_revisit'] += len(workload_revisit_debug['accepts'])
            final_score = _score_schedule(
                instances, physicians, state, targets, contract_by_physician,
                requests_by_physician_date, eligible_facilities_by_physician,
                minimum_rest_by_physician, include_internal_night_heuristics=True,
            )['score']

        night_spacing_debug = {'attempts': 0, 'accepts': [], 'stopped_reason': 'not_run'}
        if max_passes > 0 and not runtime_exceeded() and start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE:
            spacing_deadline = min(runtime_limit_seconds * 0.75, runtime_seconds_elapsed() + 20)
            maximum_started = runtime_seconds_elapsed()
            maximum_deadline = min(spacing_deadline, runtime_seconds_elapsed() + 8)
            state, _, maximum_debug = _repair_night_spacing_swaps(
                instances=instances, physicians=physicians, state=state,
                manual_pairs=manual_pairs, targets=targets,
                contract_by_physician=contract_by_physician,
                requests_by_physician_date=requests_by_physician_date,
                eligible_facilities_by_physician=eligible_facilities_by_physician,
                minimum_rest_by_physician=minimum_rest_by_physician,
                should_stop=lambda: runtime_seconds_elapsed() >= maximum_deadline or (adaptive_runtime and search_budget.reason() is not None),
                candidate_limit=600, maximum_only=True,
                on_improvement=observe_best if adaptive_runtime else None,
            )
            maximum_debug['runtime_seconds'] = runtime_seconds_elapsed() - maximum_started
            recovery_started = runtime_seconds_elapsed()
            recovery_deadline = min(spacing_deadline, recovery_started + 8)
            state, _, recovery_debug = _repair_recovery_day_swaps(
                instances=instances, physicians=physicians, state=state,
                manual_pairs=manual_pairs, targets=targets,
                contract_by_physician=contract_by_physician,
                requests_by_physician_date=requests_by_physician_date,
                eligible_facilities_by_physician=eligible_facilities_by_physician,
                minimum_rest_by_physician=minimum_rest_by_physician,
                should_stop=lambda: runtime_seconds_elapsed() >= recovery_deadline or (adaptive_runtime and search_budget.reason() is not None),
                candidate_limit=600,
                on_improvement=observe_best if adaptive_runtime else None,
            )
            recovery_debug['runtime_seconds'] = runtime_seconds_elapsed() - recovery_started
            state, _, night_spacing_debug = _run_productive_repair_rounds(
                _repair_night_spacing_swaps, elapsed=runtime_seconds_elapsed,
                deadline=spacing_deadline, candidate_limit=2000 - maximum_debug['attempts'] - recovery_debug['attempts'],
                on_improvement=observe_best if adaptive_runtime else None,
                stop_requested=search_budget.reason if adaptive_runtime else None,
                instances=instances, physicians=physicians, state=state,
                manual_pairs=manual_pairs, targets=targets,
                contract_by_physician=contract_by_physician,
                requests_by_physician_date=requests_by_physician_date,
                eligible_facilities_by_physician=eligible_facilities_by_physician,
                minimum_rest_by_physician=minimum_rest_by_physician,
            )
            night_spacing_debug['night_maximum'] = maximum_debug
            night_spacing_debug['recovery_day'] = recovery_debug
            night_spacing_debug['runtime_seconds'] = night_spacing_debug.get('runtime_seconds', 0) + recovery_debug['runtime_seconds']
            night_spacing_debug['attempts'] += recovery_debug['attempts']
            night_spacing_debug['accepts'] = recovery_debug['accepts'] + night_spacing_debug['accepts']
            night_spacing_debug['runtime_seconds'] = night_spacing_debug.get('runtime_seconds', 0) + maximum_debug['runtime_seconds']
            night_spacing_debug['attempts'] += maximum_debug['attempts']
            night_spacing_debug['accepts'] = maximum_debug['accepts'] + night_spacing_debug['accepts']
            improvement_count += len(night_spacing_debug['accepts'])
            candidates_considered_before_timeout += night_spacing_debug['attempts']
            phase_attempts['night_spacing_revisit'] += night_spacing_debug['attempts']
            phase_improvements['night_spacing_revisit'] += len(night_spacing_debug['accepts'])
            final_score = _score_schedule(
                instances, physicians, state, targets, contract_by_physician,
                requests_by_physician_date, eligible_facilities_by_physician,
                minimum_rest_by_physician, include_internal_night_heuristics=True,
            )['score']

        for _pass_number in range(max_passes):
            phase_passes_run += 1
            if runtime_exceeded():
                mark_timeout('phase_pass')
                break
            if score_is_zero():
                stopped_reason = 'score_zero'
                break
            pass_improved = False
            improved = False

            # A request-on repair has a specific intended recipient. Try that
            # assignment directly; when the requester already owns another
            # shift on the same date, evaluate the exchange as one atomic swap.
            directed_request_candidates = _request_on_repair_candidates(
                instances,
                state,
                manual_pairs,
                contract_by_physician,
                requests_by_physician_date,
            )
            for requested_physician_id, from_physician_id, instance_id in directed_request_candidates[:max_candidates_per_repair]:
                if improved:
                    break
                if runtime_exceeded():
                    mark_timeout('request_on_repair')
                    break
                candidates_considered_before_timeout += 1
                iterations_run += 1
                request_repair_attempts += 1
                phase_attempts['request_repair'] += 1
                result = try_reassign(
                    instance_id,
                    from_physician_id,
                    requested_physician_id,
                )
                if result is not None:
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        request_repair_improvements += 1
                        phase_improvements['request_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

                requested_instance = instances_by_id[instance_id]
                owned_swap_candidates = [
                    owned_instance_id
                    for owned_instance_id, physician_ids in state.items()
                    if requested_physician_id in physician_ids
                    and owned_instance_id != instance_id
                    and (owned_instance_id, requested_physician_id) not in manual_pairs
                ]
                owned_swap_candidates.sort(
                    key=lambda owned_instance_id: (
                        instances_by_id[owned_instance_id].date != requested_instance.date,
                        instances_by_id[owned_instance_id].shift_template.night_shift
                        != requested_instance.shift_template.night_shift,
                        abs(
                            _shift_hours(instances_by_id[owned_instance_id])
                            - _shift_hours(requested_instance)
                        ),
                        abs(
                            (
                                instances_by_id[owned_instance_id].date
                                - requested_instance.date
                            ).days
                        ),
                        owned_instance_id,
                    )
                )
                for owned_instance_id in owned_swap_candidates:
                    candidates_considered_before_timeout += 1
                    iterations_run += 1
                    request_repair_attempts += 1
                    phase_attempts['request_repair'] += 1
                    result = try_swap(
                        instance_id,
                        from_physician_id,
                        owned_instance_id,
                        requested_physician_id,
                    )
                    if result is None:
                        continue
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        request_repair_improvements += 1
                        phase_improvements['request_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

            request_candidates = _request_repair_candidates(
                instances,
                physicians,
                state,
                manual_pairs,
                contract_by_physician,
                requests_by_physician_date,
            )
            # Candidates are already ordered by descending request penalty so
            # FIXED requests are evaluated before lower-priority requests.
            for from_physician_id, instance_id in request_candidates[:max_candidates_per_repair]:
                if improved:
                    break
                if runtime_exceeded():
                    mark_timeout('request_repair')
                    break
                instance = instances_by_id[instance_id]
                current_for_request = _score_schedule(
                    instances,
                    physicians,
                    state,
                    targets,
                    contract_by_physician,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    include_internal_night_heuristics=True,
                )
                ordered_recipients = random_tie_sorted(
                    physicians,
                    key=lambda physician: (
                        not bool(_requests_for_shift(requests_by_physician_date, physician.id, instance)),
                        _workload_rule_delta_for_candidate(
                            instances,
                            state,
                            instances_by_id,
                            physician.id,
                            instance,
                            targets[physician.id],
                        ),
                        _workload_candidate_score(
                            targets[physician.id],
                            current_for_request['physician_hours'][physician.id] + _shift_hours(instance),
                            current_for_request['physician_shifts'][physician.id] + 1,
                        ),
                    ),
                )
                for physician in ordered_recipients[:max_candidates_per_repair]:
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('request_repair')
                        break
                    if physician.id == from_physician_id:
                        continue
                    iterations_run += 1
                    request_repair_attempts += 1
                    phase_attempts['request_repair'] += 1
                    result = try_reassign(instance_id, from_physician_id, physician.id)
                    if result is None:
                        continue
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        request_repair_improvements += 1
                        phase_improvements['request_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

            improved = False
            workload_search_improved = True
            workload_candidate_budget = max_candidates_per_repair
            while workload_search_improved and workload_candidate_budget > 0:
                if runtime_exceeded():
                    mark_timeout('workload_range_repair')
                    break
                workload_search_improved = False
                current_scoring = _score_schedule(
                    instances,
                    physicians,
                    state,
                    targets,
                    contract_by_physician,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    include_internal_night_heuristics=True,
                )
                workload_candidates = _workload_repair_candidates(
                    instances,
                    state,
                    manual_pairs,
                    current_scoring,
                )
                workload_candidates = shuffle(workload_candidates)
                for high_physician_id, low_physician_id, instance_id in workload_candidates[:workload_candidate_budget]:
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('workload_range_repair')
                        break
                    if low_physician_id in state[instance_id]:
                        continue
                    iterations_run += 1
                    workload_candidate_budget -= 1
                    workload_candidate_moves_considered += 1
                    workload_transfer_attempts += 1
                    workload_repair_attempts += 1
                    phase_attempts['workload_range_repair'] += 1
                    result = try_reassign(instance_id, high_physician_id, low_physician_id)
                    if result is None:
                        continue
                    trial_scoring, trial_state = result
                    if has_hard_invalids(trial_scoring):
                        continue
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        workload_transfer_improvements += 1
                        workload_repair_improvements += 1
                        workload_moves_accepted += 1
                        phase_improvements['workload_range_repair'] += 1
                        pass_improved = True
                        improved = True
                        workload_search_improved = True
                        break
                if stopped_reason == 'runtime_limit' or workload_search_improved:
                    continue

                current_scoring = _score_schedule(
                    instances,
                    physicians,
                    state,
                    targets,
                    contract_by_physician,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    include_internal_night_heuristics=True,
                )
                workload_swap_candidates = _workload_repair_swap_candidates(
                    instances,
                    state,
                    manual_pairs,
                    current_scoring,
                )
                workload_swap_candidates = shuffle(workload_swap_candidates)
                for high_physician_id, low_physician_id, high_instance_id, low_instance_id in workload_swap_candidates[:workload_candidate_budget]:
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('workload_range_repair')
                        break
                    iterations_run += 1
                    workload_candidate_budget -= 1
                    workload_candidate_swaps_considered += 1
                    workload_repair_attempts += 1
                    phase_attempts['workload_range_repair'] += 1
                    result = try_swap(
                        high_instance_id,
                        high_physician_id,
                        low_instance_id,
                        low_physician_id,
                    )
                    if result is None:
                        continue
                    trial_scoring, trial_state = result
                    if has_hard_invalids(trial_scoring):
                        continue
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        workload_repair_improvements += 1
                        workload_swaps_accepted += 1
                        phase_improvements['workload_range_repair'] += 1
                        pass_improved = True
                        improved = True
                        workload_search_improved = True
                        break

            improved = False

            if NIGHT_BLOCK_BUILDER_ENABLED:
                night_sources, night_counts = _night_fix_sources(
                    instances_by_id,
                    physicians,
                    state,
                    manual_pairs,
                    contract_by_physician,
                )
            else:
                night_sources, night_counts = [], {}
            low_night_physicians = [
                physician.id
                for physician in random_tie_sorted(
                    physicians,
                    key=lambda item: (
                        night_counts.get(item.id, 0),
                    ),
                )
            ]
            night_sources = shuffle(night_sources)
            for from_physician_id, instance_id in night_sources:
                if improved:
                    break
                if runtime_exceeded():
                    mark_timeout('night_minimum_repair')
                    break
                night_instance = instances_by_id[instance_id]
                ordered_night_recipients = random_tie_sorted(
                    low_night_physicians,
                    key=lambda physician_id: (
                        not _can_extend_night_block(
                            instances_by_id,
                            state,
                            contract_by_physician,
                            physician_id,
                            night_instance,
                        ),
                        night_counts.get(physician_id, 0),
                    ),
                )
                for to_physician_id in ordered_night_recipients:
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('night_minimum_repair')
                        break
                    iterations_run += 1
                    night_fix_attempts += 1
                    phase_attempts['night_minimum_repair'] += 1
                    result = try_reassign(instance_id, from_physician_id, to_physician_id)
                    if result is None:
                        continue
                    night_fix_valid_alternatives += 1
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        night_fix_improvements += 1
                        phase_improvements['night_minimum_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

            if not improved and night_sources:
                non_night_pairs = [
                    (instance_id, physician_id)
                    for instance_id, physician_id in _optimizer_pairs(state, manual_pairs)
                    if not instances_by_id[instance_id].shift_template.night_shift
                ]
                non_night_pairs = shuffle(non_night_pairs)
                for from_physician_id, night_instance_id in night_sources:
                    if improved:
                        break
                    if runtime_exceeded():
                        mark_timeout('night_minimum_repair')
                        break
                    for non_night_instance_id, low_physician_id in non_night_pairs:
                        candidates_considered_before_timeout += 1
                        if runtime_exceeded():
                            mark_timeout('night_minimum_repair')
                            break
                        if low_physician_id == from_physician_id:
                            continue
                        if night_counts.get(low_physician_id, 0) >= night_counts.get(from_physician_id, 0):
                            continue
                        iterations_run += 1
                        night_fix_attempts += 1
                        phase_attempts['night_minimum_repair'] += 1
                        night_instance = instances_by_id[night_instance_id]
                        non_night_instance = instances_by_id[non_night_instance_id]
                        trial_state = _copy_state(state)
                        _replace_in_state(
                            trial_state,
                            night_instance_id,
                            from_physician_id,
                            low_physician_id,
                        )
                        _replace_in_state(
                            trial_state,
                            non_night_instance_id,
                            low_physician_id,
                            from_physician_id,
                        )
                        if not _can_assign_in_state(
                            trial_state,
                            instances_by_id,
                            night_instance,
                            low_physician_id,
                            eligible_facilities_by_physician,
                            minimum_rest_by_physician,
                            exclude_instance_id=night_instance_id,
                        ):
                            continue
                        if not _can_assign_in_state(
                            trial_state,
                            instances_by_id,
                            non_night_instance,
                            from_physician_id,
                            eligible_facilities_by_physician,
                            minimum_rest_by_physician,
                            exclude_instance_id=non_night_instance_id,
                        ):
                            continue
                        night_fix_valid_alternatives += 1
                        trial_scoring = _score_schedule(
                            instances,
                            physicians,
                            trial_state,
                            targets,
                            contract_by_physician,
                            requests_by_physician_date,
                            eligible_facilities_by_physician,
                            minimum_rest_by_physician,
                            include_internal_night_heuristics=True,
                        )
                        if is_optimization_improvement(trial_scoring):
                            state = trial_state
                            final_score = trial_scoring['score']
                            improvement_count += 1
                            night_fix_improvements += 1
                            phase_improvements['night_minimum_repair'] += 1
                            pass_improved = True
                            improved = True
                            break

            improved = False

            current_scoring = _score_schedule(
                instances,
                physicians,
                state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            low_workload_physicians = [
                physician_id
                for _delta, physician_id in random_tie_sorted(
                    _physician_workload_rows(physician_ids, current_scoring, targets),
                    key=lambda row: row[0],
                )
            ]
            recovery_conflict_pairs = (
                shuffle(_night_recovery_conflict_pairs(
                    instances,
                    physicians,
                    state,
                    manual_pairs,
                    contract_by_physician,
                ))
                if NIGHT_BLOCK_BUILDER_ENABLED
                else []
            )
            for from_physician_id, instance_id in recovery_conflict_pairs:
                if improved:
                    break
                if runtime_exceeded():
                    mark_timeout('post_night_recovery_repair')
                    break
                for to_physician_id in low_workload_physicians:
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('post_night_recovery_repair')
                        break
                    iterations_run += 1
                    night_fix_attempts += 1
                    post_night_recovery_repair_attempts += 1
                    phase_attempts['post_night_recovery_repair'] += 1
                    result = try_reassign(instance_id, from_physician_id, to_physician_id)
                    if result is None:
                        continue
                    night_fix_valid_alternatives += 1
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        night_fix_improvements += 1
                        post_night_recovery_repair_improvements += 1
                        phase_improvements['post_night_recovery_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

            improved = False

            current_scoring = _score_schedule(
                instances,
                physicians,
                state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            low_workload_physicians = [
                physician_id
                for _delta, physician_id in random_tie_sorted(
                    _physician_workload_rows(physician_ids, current_scoring, targets),
                    key=lambda row: row[0],
                )
            ]
            same_shift_candidates = shuffle(_same_shift_break_candidates(
                instances,
                physicians,
                state,
                manual_pairs,
                contract_by_physician,
            ))
            same_shift_started = runtime_seconds_elapsed()
            same_shift_deadline = (same_shift_started + 3 if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                                   else runtime_limit_seconds)
            for from_physician_id, instance_id in same_shift_candidates:
                if improved or runtime_seconds_elapsed() >= same_shift_deadline:
                    break
                if runtime_exceeded():
                    mark_timeout('same_shift_repair')
                    break
                for to_physician_id in low_workload_physicians:
                    if runtime_seconds_elapsed() >= same_shift_deadline:
                        break
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('same_shift_repair')
                        break
                    iterations_run += 1
                    same_shift_break_attempts += 1
                    phase_attempts['same_shift_repair'] += 1
                    result = try_reassign(instance_id, from_physician_id, to_physician_id)
                    if result is None:
                        continue
                    same_shift_fix_valid_alternatives += 1
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        same_shift_break_improvements += 1
                        phase_improvements['same_shift_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

            phase_runtime_seconds['same_shift_repair'] += runtime_seconds_elapsed() - same_shift_started
            improved = False

            current_scoring = _score_schedule(
                instances,
                physicians,
                state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            low_workload_physicians = [
                physician_id
                for _delta, physician_id in random_tie_sorted(
                    _physician_workload_rows(physician_ids, current_scoring, targets),
                    key=lambda row: row[0],
                )
            ]
            consecutive_day_candidates = shuffle(_consecutive_day_break_candidates(
                state,
                instances_by_id,
                manual_pairs,
                contract_by_physician,
            ))
            consecutive_started = runtime_seconds_elapsed()
            consecutive_deadline = (consecutive_started + 3 if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                                    else runtime_limit_seconds)
            for from_physician_id, instance_id in consecutive_day_candidates:
                if improved or runtime_seconds_elapsed() >= consecutive_deadline:
                    break
                if runtime_exceeded():
                    mark_timeout('consecutive_day_repair')
                    break
                for to_physician_id in low_workload_physicians:
                    if runtime_seconds_elapsed() >= consecutive_deadline:
                        break
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('consecutive_day_repair')
                        break
                    iterations_run += 1
                    consecutive_day_break_attempts += 1
                    phase_attempts['consecutive_day_repair'] += 1
                    result = try_reassign(instance_id, from_physician_id, to_physician_id)
                    if result is None:
                        continue
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        consecutive_day_break_improvements += 1
                        phase_improvements['consecutive_day_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

            improved = False

            phase_runtime_seconds['consecutive_day_repair'] += runtime_seconds_elapsed() - consecutive_started
            pairs = _optimizer_pairs(state, manual_pairs)
            pairs = shuffle(pairs)
            swap_attempts_this_pass = 0
            for left_index, (left_instance_id, left_physician_id) in enumerate(pairs):
                if runtime_exceeded():
                    mark_timeout('general_hill_climb_swaps')
                    break
                if swap_attempts_this_pass >= max_swap_attempts_per_pass:
                    break
                for right_instance_id, right_physician_id in pairs[left_index + 1:]:
                    candidates_considered_before_timeout += 1
                    if runtime_exceeded():
                        mark_timeout('general_hill_climb_swaps')
                        break
                    if swap_attempts_this_pass >= max_swap_attempts_per_pass:
                        break
                    if left_instance_id == right_instance_id:
                        continue
                    if left_physician_id == right_physician_id:
                        continue
                    iterations_run += 1
                    swap_attempts_this_pass += 1
                    swap_attempts += 1
                    general_swap_attempts += 1
                    phase_attempts['general_hill_climb_swaps'] += 1
                    left_instance = instances_by_id[left_instance_id]
                    right_instance = instances_by_id[right_instance_id]
                    trial_state = _copy_state(state)
                    _replace_in_state(
                        trial_state,
                        left_instance_id,
                        left_physician_id,
                        right_physician_id,
                    )
                    _replace_in_state(
                        trial_state,
                        right_instance_id,
                        right_physician_id,
                        left_physician_id,
                    )
                    if not _can_assign_in_state(
                        trial_state,
                        instances_by_id,
                        left_instance,
                        right_physician_id,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        exclude_instance_id=left_instance_id,
                    ):
                        continue
                    if not _can_assign_in_state(
                        trial_state,
                        instances_by_id,
                        right_instance,
                        left_physician_id,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        exclude_instance_id=right_instance_id,
                    ):
                        continue
                    trial_scoring = _score_schedule(
                        instances,
                        physicians,
                        trial_state,
                        targets,
                        contract_by_physician,
                        requests_by_physician_date,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        include_internal_night_heuristics=True,
                    )
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        swap_improvements += 1
                        general_swap_improvements += 1
                        phase_improvements['general_hill_climb_swaps'] += 1
                        pass_improved = True
                        improved = True
                        break
                if improved:
                    break
            if stopped_reason == 'runtime_limit':
                break

            if not pass_improved and not improved:
                stopped_reason = 'no_improvement'
                break

        if phase_passes_run >= max_passes and stopped_reason == 'no_improvement' and final_score > Decimal('0'):
            stopped_reason = 'max_phase_passes'

        current_scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
            include_internal_night_heuristics=True,
        )
        final_score = current_scoring['score']

        def repair_rejection_state(physician_id):
            return {
                'physician_id': physician_id,
                'physician': _physician_display_name(physicians_by_id[physician_id]),
                'candidate_nights_considered': 0,
                'valid_candidates_found': 0,
                'best_rejected_move_score_delta': None,
                'rejection_categories': {
                    'facility_ineligible': 0,
                    'overlap': 0,
                    'rest': 0,
                    'recovery': 0,
                    'max_consecutive_nights': 0,
                    'same_shift': 0,
                    'worsened_total_score': 0,
                },
            }

        def add_rejection(debug_row, category, delta=None):
            debug_row['rejection_categories'][category] += 1
            if delta is not None:
                current_best = debug_row['best_rejected_move_score_delta']
                if current_best is None or delta < current_best:
                    debug_row['best_rejected_move_score_delta'] = float(delta)

        def assignment_rejection_category(trial_state, instance, physician_id, exclude_instance_id):
            if instance.facility_id not in eligible_facilities_by_physician.get(physician_id, set()):
                return 'facility_ineligible'
            intervals = _intervals_for_physician(
                trial_state,
                instances_by_id,
                physician_id,
                exclude_instance_id=exclude_instance_id,
            )
            if _overlaps(instance, intervals):
                return 'overlap'
            if _rest_violation(
                instance,
                intervals,
                minimum_rest_by_physician[physician_id],
            ):
                return 'rest'
            return None

        def night_rule_rejection_category(trial_state, physician_ids_to_check):
            trial_report = _night_violation_report(
                instances,
                physicians,
                trial_state,
                contract_by_physician,
            )
            for violation in trial_report['night_violations']:
                if violation['physician_id'] not in physician_ids_to_check:
                    continue
                if violation['violation_type'] == 'MAX_CONSECUTIVE_NIGHTS':
                    return 'max_consecutive_nights'
                if violation['violation_type'] in {
                    'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT',
                    'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK',
                }:
                    return 'recovery'
            return None

        def score_repair_trial(trial_state, checked_physician_ids, current_same_shift_score, debug_row):
            category = night_rule_rejection_category(trial_state, checked_physician_ids)
            if category is not None:
                add_rejection(debug_row, category)
                return None
            trial_scoring = _score_schedule(
                instances,
                physicians,
                trial_state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            validation = trial_scoring['validation']
            if validation['final_overlap_violations']:
                add_rejection(debug_row, 'overlap')
                return None
            if validation['final_rest_violations']:
                add_rejection(debug_row, 'rest')
                return None
            if trial_scoring['breakdown']['same_shift_score'] > current_same_shift_score:
                add_rejection(debug_row, 'same_shift')
                return None
            debug_row['valid_candidates_found'] += 1
            delta = trial_scoring['score'] - final_score
            if delta >= 0:
                add_rejection(debug_row, 'worsened_total_score', delta=delta)
                return None
            return trial_scoring, trial_state

        if NIGHT_BLOCK_BUILDER_ENABLED:
            repair_status = _night_minimum_status(
                instances,
                physicians,
                state,
                contract_by_physician,
            )
            under_rows = shuffle(repair_status['physicians_under_night_minimum'])
        else:
            under_rows = []
        for under_row in under_rows:
            if runtime_exceeded():
                mark_timeout('night_minimum_repair_debug')
                break
            to_physician_id = under_row['physician_id']
            debug_row = repair_rejection_state(to_physician_id)
            current_scoring = _score_schedule(
                instances,
                physicians,
                state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                include_internal_night_heuristics=True,
            )
            current_same_shift_score = current_scoring['breakdown']['same_shift_score']
            current_minimum_status = _night_minimum_status(
                instances,
                physicians,
                state,
                contract_by_physician,
            )
            current_under_ids = {
                row['physician_id']
                for row in current_minimum_status['physicians_under_night_minimum']
            }
            night_counts = {
                item['physician_id']: item['night_shifts']
                for item in _night_violation_report(
                    instances,
                    physicians,
                    state,
                    contract_by_physician,
                )['night_shifts_by_physician']
            }
            donor_night_pairs = [
                (night_counts.get(physician_id, 0), instance_id, physician_id)
                for instance_id, physician_id in _optimizer_pairs(state, manual_pairs)
                if physician_id != to_physician_id
                and physician_id not in current_under_ids
                and instances_by_id[instance_id].shift_template.night_shift
                and night_counts.get(physician_id, 0) > night_counts.get(to_physician_id, 0)
            ]
            donor_night_pairs = random_tie_sorted(
                donor_night_pairs,
                key=lambda item: (-item[0], item[1]),
            )

            repaired = False
            for _night_count, night_instance_id, donor_physician_id in donor_night_pairs:
                candidates_considered_before_timeout += 1
                if runtime_exceeded():
                    mark_timeout('night_minimum_repair_debug')
                    break
                night_minimum_fix_attempts += 1
                night_minimum_direct_reassignment_attempts += 1
                phase_attempts['night_minimum_repair'] += 1
                debug_row['candidate_nights_considered'] += 1
                trial_state = _copy_state(state)
                _replace_in_state(
                    trial_state,
                    night_instance_id,
                    donor_physician_id,
                    to_physician_id,
                )
                night_instance = instances_by_id[night_instance_id]
                category = assignment_rejection_category(
                    trial_state,
                    night_instance,
                    to_physician_id,
                    night_instance_id,
                )
                if category is not None:
                    add_rejection(debug_row, category)
                    continue
                result = score_repair_trial(
                    trial_state,
                    {to_physician_id, donor_physician_id},
                    current_same_shift_score,
                    debug_row,
                )
                if result is None:
                    continue
                night_minimum_fix_valid_alternatives += 1
                trial_scoring, trial_state = result
                state = trial_state
                final_score = trial_scoring['score']
                improvement_count += 1
                night_minimum_fix_improvements += 1
                night_minimum_direct_reassignment_successes += 1
                phase_improvements['night_minimum_repair'] += 1
                repaired = True
                break

            if not repaired:
                non_night_pairs = [
                    (instance_id, physician_id)
                    for instance_id, physician_id in _optimizer_pairs(state, manual_pairs)
                    if physician_id == to_physician_id
                    and not instances_by_id[instance_id].shift_template.night_shift
                ]
                non_night_pairs = shuffle(non_night_pairs)
                for non_night_instance_id, under_physician_id in non_night_pairs:
                    if repaired:
                        break
                    if runtime_exceeded():
                        mark_timeout('night_minimum_repair_debug')
                        break
                    for _night_count, night_instance_id, donor_physician_id in donor_night_pairs:
                        candidates_considered_before_timeout += 1
                        if runtime_exceeded():
                            mark_timeout('night_minimum_repair_debug')
                            break
                        night_minimum_fix_attempts += 1
                        night_minimum_swap_attempts += 1
                        phase_attempts['night_minimum_repair'] += 1
                        debug_row['candidate_nights_considered'] += 1
                        trial_state = _copy_state(state)
                        _replace_in_state(
                            trial_state,
                            night_instance_id,
                            donor_physician_id,
                            under_physician_id,
                        )
                        _replace_in_state(
                            trial_state,
                            non_night_instance_id,
                            under_physician_id,
                            donor_physician_id,
                        )
                        night_instance = instances_by_id[night_instance_id]
                        non_night_instance = instances_by_id[non_night_instance_id]
                        category = assignment_rejection_category(
                            trial_state,
                            night_instance,
                            under_physician_id,
                            night_instance_id,
                        )
                        if category is not None:
                            add_rejection(debug_row, category)
                            continue
                        category = assignment_rejection_category(
                            trial_state,
                            non_night_instance,
                            donor_physician_id,
                            non_night_instance_id,
                        )
                        if category is not None:
                            add_rejection(debug_row, category)
                            continue
                        result = score_repair_trial(
                            trial_state,
                            {under_physician_id, donor_physician_id},
                            current_same_shift_score,
                            debug_row,
                        )
                        if result is None:
                            continue
                        night_minimum_fix_valid_alternatives += 1
                        trial_scoring, trial_state = result
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        night_minimum_fix_improvements += 1
                        night_minimum_swap_successes += 1
                        phase_improvements['night_minimum_repair'] += 1
                        repaired = True
                        break

            if not repaired:
                debug_row['reason'] = (
                    'No legal improving direct reassignment or swap was found for this physician.'
                )
                night_minimum_repair_debug.append(debug_row)

        plateau_scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
        )
        final_score = plateau_scoring['score']
        score_before_final_plateau_repair = float(final_score)

        def try_final_plateau_reassign(
            instance_id, from_physician_id, to_physician_id, *, require_diagnostics=False,
        ):
            nonlocal reassignment_candidates_rejected_incrementally
            nonlocal reassignment_candidates_full_score_verified
            if (
                to_physician_id == from_physician_id
                or (instance_id, from_physician_id) in manual_pairs
                or to_physician_id in state[instance_id]
            ):
                return None
            instance = instances_by_id[instance_id]
            trial_state = _copy_state(state)
            _replace_in_state(
                trial_state, instance_id, from_physician_id, to_physician_id,
            )
            if not _can_assign_in_state(
                trial_state,
                instances_by_id,
                instance,
                to_physician_id,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
                exclude_instance_id=instance_id,
            ):
                return None
            incremental_delta = _selected_physician_score_delta(
                instances,
                physicians,
                state,
                trial_state,
                {from_physician_id, to_physician_id},
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
            )
            if incremental_delta >= 0 and not require_diagnostics:
                reassignment_candidates_rejected_incrementally += 1
                return None
            reassignment_candidates_full_score_verified += 1
            trial_scoring = _score_schedule(
                instances,
                physicians,
                trial_state,
                targets,
                contract_by_physician,
                requests_by_physician_date,
                eligible_facilities_by_physician,
                minimum_rest_by_physician,
            )
            if has_hard_invalids(trial_scoring):
                return None
            return trial_scoring, trial_state

        plateau_candidate_limit = max_candidates_per_repair * 2
        plateau_improved = False
        violating_workload_ids = set()
        targeted_pairs = []

        # The ordinary workload repair starts with physicians above maximum.  A
        # reduced-contract schedule can instead have a large below-minimum
        # penalty while every current owner is inside range.  Mirror the
        # lightweight feasibility diagnostic's individual-shift neighborhood:
        # inspect legal unlocked transfers for the worst under-minimum physician
        # first, but use the complete official score as the acceptance rule.
        minimum_repair_attempt_limit = min(
            2000, max(500, plateau_candidate_limit * 10),
        )

        def workload_row_by_physician(scoring):
            return {
                row['physician_id']: row
                for row in scoring.get('workload_score_rows', [])
            }

        def workload_contribution(row):
            if row is None:
                return Decimal('0')
            return Decimal(str(
                row.get('score_contribution_exact', row.get('score_contribution', 0))
            ))

        def night_maximum_penalty(scoring, physician_id):
            return sum(
                Decimal(str(row.get('penalty', 0)))
                for row in scoring.get('night_violations', [])
                if row.get('physician_id') == physician_id
                and row.get('violation_type') == 'NIGHT_OVER_MAXIMUM'
            )

        def record_minimum_rejection(reason, physician_name):
            workload_minimum_repair_rejected_reasons[reason] += 1
            if 'turner' in (physician_name or '').lower():
                workload_minimum_repair_turner_summary['rejected_reasons'][reason] += 1

        minimum_repair_exhausted = False
        while (
            not runtime_exceeded()
            and workload_minimum_repair_candidates_considered < minimum_repair_attempt_limit
        ):
            current_rows = workload_row_by_physician(plateau_scoring)
            under_rows = sorted(
                [
                    row for row in current_rows.values()
                    if row.get('deviation_direction') == 'below_minimum'
                ],
                key=lambda row: (
                    -workload_contribution(row),
                    -Decimal(str(row.get('deviation', 0))),
                    row['physician_id'],
                ),
            )
            if not under_rows or final_score <= 0:
                break
            accepted_in_round = False
            for under_row in under_rows:
                if runtime_exceeded():
                    break
                to_physician_id = under_row['physician_id']
                physician = next(
                    item for item in physicians if item.id == to_physician_id
                )
                physician_name = _physician_display_name(physician)
                workload_minimum_repair_physicians_considered += 1
                if 'turner' in physician_name.lower():
                    workload_minimum_repair_turner_summary['physicians'].append({
                        'physician_id': to_physician_id,
                        'physician': physician_name,
                        'assigned_hours': under_row.get('assigned_hours'),
                        'allowed_min': under_row.get('allowed_min'),
                        'allowed_max': under_row.get('allowed_max'),
                        'workload_score_contribution': float(workload_contribution(under_row)),
                    })
                deficit = Decimal(str(under_row.get('deviation', 0)))
                ordered_instances = sorted(
                    instances,
                    key=lambda item: (
                        abs(_shift_hours(item) - deficit),
                        item.date,
                        item.start_datetime,
                        item.id,
                    ),
                )
                for instance in ordered_instances:
                    if (
                        runtime_exceeded()
                        or workload_minimum_repair_candidates_considered
                        >= minimum_repair_attempt_limit
                    ):
                        break
                    if to_physician_id in state[instance.id]:
                        record_minimum_rejection('already_assigned', physician_name)
                        continue
                    if instance.id in source_locked_open_ids:
                        record_minimum_rejection('locked_assignment', physician_name)
                        continue
                    if instance.facility_id not in eligible_facilities_by_physician.get(
                        to_physician_id, set(),
                    ):
                        record_minimum_rejection('facility_ineligible', physician_name)
                        continue
                    matching_requests = _requests_for_shift(
                        requests_by_physician_date, to_physician_id, instance,
                    )
                    if any(
                        request.request_type in {
                            ScheduleRequest.RequestType.DAY_OFF,
                            ScheduleRequest.RequestType.SHIFT_OFF,
                        }
                        for request in matching_requests
                    ):
                        record_minimum_rejection('request_conflict', physician_name)
                        continue

                    movable_owners = [
                        owner_id for owner_id in state[instance.id]
                        if (instance.id, owner_id) not in manual_pairs
                    ]
                    if not movable_owners:
                        record_minimum_rejection('locked_assignment', physician_name)
                        continue
                    # Prefer an owner whose current workload penalty can absorb
                    # the transfer, while still allowing full-score evaluation
                    # to make the final decision.
                    movable_owners.sort(key=lambda owner_id: (
                        0 if current_rows.get(owner_id, {}).get('deviation_direction') == 'above_maximum' else 1,
                        -workload_contribution(current_rows.get(owner_id)),
                        owner_id,
                    ))
                    for from_physician_id in movable_owners:
                        if (
                            runtime_exceeded()
                            or workload_minimum_repair_candidates_considered
                            >= minimum_repair_attempt_limit
                        ):
                            break
                        workload_minimum_repair_candidates_considered += 1
                        workload_candidate_moves_considered += 1
                        phase_attempts['workload_minimum_repair'] += 1
                        if 'turner' in physician_name.lower():
                            workload_minimum_repair_turner_summary['candidates_considered'] += 1
                        result = try_final_plateau_reassign(
                            instance.id, from_physician_id, to_physician_id,
                            require_diagnostics=True,
                        )
                        if result is None:
                            # Facility was checked above, so the remaining shared
                            # assignment guard is overlap/rest (or an invalid
                            # source state). Keep the diagnostic reason explicit.
                            trial_intervals = [
                                (instances_by_id[assigned_id].start_datetime,
                                 instances_by_id[assigned_id].end_datetime)
                                for assigned_id, assigned_physician_ids in state.items()
                                for assigned_physician_id in assigned_physician_ids
                                if assigned_physician_id == to_physician_id
                                and assigned_id != instance.id
                            ]
                            reason = (
                                'overlap' if _overlaps(instance, trial_intervals)
                                else 'rest' if _rest_violation(
                                    instance, trial_intervals,
                                    minimum_rest_by_physician[to_physician_id],
                                ) else 'illegal_or_locked_move'
                            )
                            record_minimum_rejection(reason, physician_name)
                            continue
                        trial_scoring, trial_state = result
                        trial_rows = workload_row_by_physician(trial_scoring)
                        trial_receiver = trial_rows.get(to_physician_id)
                        if trial_receiver and trial_receiver.get('deviation_direction') == 'above_maximum':
                            record_minimum_rejection('would_exceed_max_workload', physician_name)
                            continue
                        if (
                            instance.shift_template.night_shift
                            and night_maximum_penalty(trial_scoring, to_physician_id)
                            > night_maximum_penalty(plateau_scoring, to_physician_id)
                        ):
                            record_minimum_rejection('night_maximum', physician_name)
                            continue

                        recipient_gain = (
                            workload_contribution(current_rows.get(to_physician_id))
                            - workload_contribution(trial_rows.get(to_physician_id))
                        )
                        donor_loss = (
                            workload_contribution(trial_rows.get(from_physician_id))
                            - workload_contribution(current_rows.get(from_physician_id))
                        )
                        donor_remains_in_range = (
                            trial_rows.get(from_physician_id, {}).get('deviation_direction')
                            == 'inside_range'
                        )
                        if not donor_remains_in_range and donor_loss >= recipient_gain:
                            record_minimum_rejection('donor_workload_tradeoff', physician_name)
                            continue
                        delta = trial_scoring['score'] - final_score
                        if (
                            workload_minimum_repair_best_delta is None
                            or delta < workload_minimum_repair_best_delta
                        ):
                            workload_minimum_repair_best_delta = delta
                        if delta >= 0:
                            record_minimum_rejection('would_not_lower_total_score', physician_name)
                            continue

                        score_before_move = final_score
                        state = trial_state
                        plateau_scoring = trial_scoring
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        workload_minimum_repair_accepts += 1
                        workload_moves_accepted += 1
                        phase_improvements['workload_minimum_repair'] += 1
                        plateau_improved = True
                        accepted_in_round = True
                        if 'turner' in physician_name.lower():
                            workload_minimum_repair_turner_summary['accepts'].append({
                                'shift_instance_id': instance.id,
                                'from_physician_id': from_physician_id,
                                'to_physician_id': to_physician_id,
                                'hours': float(_shift_hours(instance)),
                                'score_before': float(score_before_move),
                                'score_after': float(final_score),
                                'score_delta': float(delta),
                            })
                        break
                    if accepted_in_round:
                        break
                if accepted_in_round:
                    break
            if not accepted_in_round:
                minimum_repair_exhausted = True
                break

        weekend_had_targets = False
        if not runtime_exceeded():
            final_plateau_repair_reason = 'no_improving_candidate'
            workload_rows = plateau_scoring.get('workload_score_rows', [])
            over_max_physicians_considered = [
                {
                    'physician_id': row['physician_id'],
                    'physician': row.get('physician'),
                    'assigned_hours': row.get('assigned_hours'),
                    'assigned_shifts': row.get('assigned_shifts'),
                    'allowed_max': row.get('allowed_max'),
                    'allowed_units': row.get('allowed_units'),
                    'deviation': row.get('deviation'),
                }
                for row in workload_rows
                if row.get('deviation_direction') == 'above_maximum'
            ]
            violating_workload_ids = {
                row['physician_id']
                for row in workload_rows
                if row.get('deviation_direction') in {'above_maximum', 'below_minimum'}
            }
            workload_candidates = [
                (instance_id, from_physician_id, to_physician_id)
                for from_physician_id, to_physician_id, instance_id
                in _workload_repair_candidates(
                    instances, state, manual_pairs, plateau_scoring,
                )
            ]
            below_minimum_ids = {
                row['physician_id'] for row in workload_rows
                if row.get('deviation_direction') == 'below_minimum'
            }
            for instance_id, from_physician_id in _optimizer_pairs(state, manual_pairs):
                for to_physician_id in below_minimum_ids:
                    workload_candidates.append(
                        (instance_id, from_physician_id, to_physician_id)
                    )
            workload_candidates = list(dict.fromkeys(workload_candidates))
            for instance_id, from_physician_id, to_physician_id in workload_candidates[:plateau_candidate_limit]:
                if runtime_exceeded():
                    mark_timeout('final_plateau_repair')
                    final_plateau_repair_reason = 'runtime_limit'
                    break
                final_plateau_repair_attempts += 1
                workload_micro_repairs_attempted += 1
                workload_candidate_moves_considered += 1
                phase_attempts['final_plateau_repair'] += 1
                result = try_final_plateau_reassign(
                    instance_id, from_physician_id, to_physician_id,
                )
                if result is None:
                    workload_micro_repair_best_rejected_reason = (
                        workload_micro_repair_best_rejected_reason or 'illegal_or_locked_move'
                    )
                    continue
                trial_scoring, trial_state = result
                delta = trial_scoring['score'] - final_score
                if workload_micro_repair_best_delta is None or delta < workload_micro_repair_best_delta:
                    workload_micro_repair_best_delta = delta
                if delta < 0:
                    score_before_move = final_score
                    workload_before_move = plateau_scoring['breakdown']['workload_score']
                    state = trial_state
                    plateau_scoring = trial_scoring
                    final_score = trial_scoring['score']
                    improvement_count += 1
                    final_plateau_repair_accepts += 1
                    workload_micro_repairs_accepted += 1
                    phase_improvements['final_plateau_repair'] += 1
                    plateau_improved = True
                    final_plateau_repair_reason = 'improved'
                    workload_micro_repair_accepted_details.append({
                        'action': 'move',
                        'shift_instance_id': instance_id,
                        'from_physician_id': from_physician_id,
                        'to_physician_id': to_physician_id,
                        'score_before': float(score_before_move),
                        'score_after': float(final_score),
                        'score_delta': float(delta),
                        'workload_score_delta': float(
                            trial_scoring['breakdown']['workload_score'] - workload_before_move
                        ),
                    })
                else:
                    workload_micro_repair_best_rejected_reason = 'would_not_lower_total_score'

            workload_swap_limit = plateau_candidate_limit * 10
            for (
                over_physician_id, receiver_physician_id,
                over_instance_id, receiver_instance_id,
            ) in _workload_repair_swap_candidates(
                instances, state, manual_pairs, plateau_scoring,
            )[:workload_swap_limit]:
                if runtime_exceeded():
                    mark_timeout('final_plateau_repair')
                    final_plateau_repair_reason = 'runtime_limit'
                    break
                workload_candidate_swaps_considered += 1
                workload_micro_repairs_attempted += 1
                pairwise_swaps_attempted += 1
                final_plateau_repair_attempts += 1
                phase_attempts['final_plateau_repair'] += 1
                result = evaluate_plateau_pairwise_swap(
                    instances=instances, physicians=physicians, state=state,
                    instances_by_id=instances_by_id, manual_pairs=manual_pairs,
                    locked_open_instance_ids=set(source_locked_open_ids), targets=targets,
                    contract_by_physician=contract_by_physician,
                    requests_by_physician_date=requests_by_physician_date,
                    eligible_facilities_by_physician=eligible_facilities_by_physician,
                    minimum_rest_by_physician=minimum_rest_by_physician,
                    current_score=final_score, left_instance_id=over_instance_id,
                    left_physician_id=over_physician_id,
                    right_instance_id=receiver_instance_id,
                    right_physician_id=receiver_physician_id,
                )
                if not result['legal']:
                    workload_micro_repair_best_rejected_reason = result['reason']
                    continue
                delta = result['score_delta']
                if best_pairwise_swap_delta is None or delta < best_pairwise_swap_delta:
                    best_pairwise_swap_delta = delta
                if workload_micro_repair_best_delta is None or delta < workload_micro_repair_best_delta:
                    workload_micro_repair_best_delta = delta
                if not result['improving']:
                    workload_micro_repair_best_rejected_reason = (
                        'would_worsen_total_score' if delta > 0 else 'would_not_lower_total_score'
                    )
                    continue
                score_before_swap = final_score
                workload_before_swap = plateau_scoring['breakdown']['workload_score']
                state = result['state']
                plateau_scoring = result['scoring']
                final_score = result['scoring']['score']
                improvement_count += 1
                final_plateau_repair_accepts += 1
                workload_micro_repairs_accepted += 1
                pairwise_swaps_accepted += 1
                phase_improvements['final_plateau_repair'] += 1
                plateau_improved = True
                final_plateau_repair_reason = 'improved'
                detail = {
                    'action': 'swap',
                    'left_shift_instance_id': over_instance_id,
                    'left_physician_id': over_physician_id,
                    'right_shift_instance_id': receiver_instance_id,
                    'right_physician_id': receiver_physician_id,
                    'score_before': float(score_before_swap),
                    'score_after': float(final_score),
                    'score_delta': float(delta),
                    'workload_score_delta': float(
                        result['scoring']['breakdown']['workload_score'] - workload_before_swap
                    ),
                }
                workload_micro_repair_accepted_details.append(detail)
                accepted_pairwise_swap_details.append(detail)
                break

        # Weekend-only plateaus previously fell through this phase because its
        # target set contained only workload and night violations.  Search the
        # assignments named by the authoritative weekend report and rescan
        # after every accepted swap.  Candidate ordering favors moves that join
        # another configured weekend-designated assignment, but the complete
        # official score remains the acceptance rule.  No weekday is assumed
        # to be a weekend here; Shift Builder's per-template selections decide.
        if not runtime_exceeded():
            weekend_attempt_limit = min(
                12000, max(4000, plateau_candidate_limit * 60),
            )
            weekend_round_limit = 12

            weekend_date_counts_by_physician = {}

            def assigned_weekend_dates(physician_id, exclude_instance_id=None):
                date_counts = weekend_date_counts_by_physician.get(
                    physician_id, Counter(),
                )
                excluded_date = None
                if exclude_instance_id is not None:
                    excluded_instance = instances_by_id[exclude_instance_id]
                    if (
                        physician_id in state.get(exclude_instance_id, ())
                        and _is_weekend_designated(excluded_instance)
                    ):
                        excluded_date = excluded_instance.date
                return {
                    day
                    for day, count in date_counts.items()
                    if count - int(day == excluded_date) > 0
                }

            def joins_configured_weekend(day, existing_days):
                if any(abs((day - other_day).days) == 1 for other_day in existing_days):
                    return True
                day_week = day - timedelta(days=day.weekday())
                return any(
                    abs((day_week - (
                        other_day - timedelta(days=other_day.weekday())
                    )).days) == 7
                    for other_day in existing_days
                )

            while (
                not runtime_exceeded()
                and weekend_repair_rescan_rounds < weekend_round_limit
                and weekend_repairs_attempted < weekend_attempt_limit
            ):
                weekend_report = _weekend_volume_report(
                    instances, physicians, state, contract_by_physician,
                    details=True,
                )
                anchor_targets = []
                seen_anchors = set()
                for violation in sorted(
                    weekend_report.get('violations', ()),
                    key=lambda row: -Decimal(str(row.get('penalty', 0))),
                ):
                    physician_id = violation.get('physician_id')
                    violation_type = violation.get('violation_type') or ''
                    for instance_id in violation.get('shift_instance_ids') or ():
                        anchor = (instance_id, physician_id, violation_type)
                        if (
                            physician_id in state.get(instance_id, ())
                            and (instance_id, physician_id) not in manual_pairs
                            and anchor not in seen_anchors
                        ):
                            seen_anchors.add(anchor)
                            anchor_targets.append(anchor)
                if not anchor_targets:
                    break
                weekend_had_targets = True
                weekend_repair_rescan_rounds += 1
                movable_pairs = _optimizer_pairs(state, manual_pairs)
                movable_by_physician = defaultdict(list)
                for pair in movable_pairs:
                    movable_by_physician[pair[1]].append(pair)
                weekend_date_counts_by_physician = defaultdict(Counter)
                for assigned_instance_id, assigned_physician_ids in state.items():
                    assigned_instance = instances_by_id[assigned_instance_id]
                    if not _is_weekend_designated(assigned_instance):
                        continue
                    for assigned_physician_id in assigned_physician_ids:
                        weekend_date_counts_by_physician[
                            assigned_physician_id
                        ][assigned_instance.date] += 1

                # For minimum-block and Friday-before-weekend violations, the
                # isolated assignment is an anchor that should normally stay
                # in place. Exchange one of that physician's other assignments
                # for an adjacent configured-weekend shift. Moving the anchor
                # itself simply recreates a one-day block elsewhere.
                weekend_sources = []
                for anchor_instance_id, physician_id, violation_type in anchor_targets:
                    if runtime_exceeded():
                        mark_timeout('final_plateau_repair')
                        final_plateau_repair_reason = 'runtime_limit'
                        break
                    anchor_pair = (anchor_instance_id, physician_id)
                    support_pairs = [
                        pair for pair in movable_by_physician[physician_id]
                        if pair != anchor_pair
                    ]
                    support_pairs.sort(key=lambda pair: (
                        0 if not _is_weekend_designated(
                            instances_by_id[pair[0]]
                        ) else 1,
                        abs((
                            instances_by_id[pair[0]].date
                            - instances_by_id[anchor_instance_id].date
                        ).days),
                        pair[0],
                    ))
                    support_pairs = support_pairs[:20]
                    if (
                        violation_type.startswith('MIN_')
                        or violation_type == 'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF'
                    ):
                        source_pairs = [*support_pairs, anchor_pair]
                    else:
                        source_pairs = [anchor_pair, *support_pairs]
                    weekend_sources.extend(
                        (anchor_instance_id, violation_type, pair)
                        for pair in source_pairs
                    )
                weekend_sources = list(dict.fromkeys(weekend_sources))
                ordered_rights = {}
                for anchor_instance_id, violation_type, left_pair in weekend_sources:
                    if runtime_exceeded():
                        mark_timeout('final_plateau_repair')
                        final_plateau_repair_reason = 'runtime_limit'
                        break
                    left_instance_id, left_physician_id = left_pair
                    anchor_instance = instances_by_id[anchor_instance_id]
                    left_instance = instances_by_id[left_instance_id]
                    left_remaining = assigned_weekend_dates(
                        left_physician_id, left_instance_id,
                    )
                    candidates = []
                    for candidate_index, (
                        right_instance_id, right_physician_id,
                    ) in enumerate(movable_pairs):
                        if candidate_index % 64 == 0 and runtime_exceeded():
                            mark_timeout('final_plateau_repair')
                            final_plateau_repair_reason = 'runtime_limit'
                            break
                        if (
                            right_instance_id == left_instance_id
                            or right_physician_id == left_physician_id
                        ):
                            continue
                        right_instance = instances_by_id[right_instance_id]
                        day_delta = abs((
                            right_instance.date - anchor_instance.date
                        ).days)
                        if (
                            violation_type == 'MIN_CONSECUTIVE_WEEKEND_SHIFTS'
                            and (
                                not _is_weekend_designated(right_instance)
                                or day_delta != 1
                            )
                        ):
                            continue
                        if (
                            violation_type == 'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF'
                            and (
                                not _is_weekend_designated(right_instance)
                                or day_delta not in {1, 2}
                            )
                        ):
                            continue
                        if (
                            violation_type == 'MIN_CONSECUTIVE_WEEKENDS'
                            and (
                                not _is_weekend_designated(right_instance)
                                or abs((
                                    right_instance.date
                                    - timedelta(days=right_instance.date.weekday())
                                    - (
                                        anchor_instance.date
                                        - timedelta(days=anchor_instance.date.weekday())
                                    )
                                ).days) != 7
                            )
                        ):
                            continue
                        right_remaining = assigned_weekend_dates(
                            right_physician_id, right_instance_id,
                        )
                        right_joins_left = (
                            _is_weekend_designated(right_instance)
                            and joins_configured_weekend(
                                right_instance.date,
                                left_remaining | {anchor_instance.date},
                            )
                        )
                        left_joins_right = (
                            _is_weekend_designated(left_instance)
                            and joins_configured_weekend(
                                left_instance.date, right_remaining,
                            )
                        )
                        candidates.append((
                            (
                                -(int(right_joins_left) + int(left_joins_right)),
                                0 if abs((
                                    right_instance.date - anchor_instance.date
                                ).days) == 1 else 1,
                                0 if right_joins_left else 1,
                                0 if _is_weekend_designated(right_instance) else 1,
                                abs(
                                    _shift_hours(right_instance)
                                    - _shift_hours(left_instance)
                                ),
                                abs((right_instance.date - anchor_instance.date).days),
                                right_physician_id,
                                right_instance_id,
                            ),
                            (right_instance_id, right_physician_id),
                        ))
                    source_key = (
                        anchor_instance_id, violation_type,
                        left_instance_id, left_physician_id,
                    )
                    if runtime_exceeded():
                        break
                    ordered_rights[source_key] = [
                        pair for _priority, pair in sorted(candidates)
                    ]

                if runtime_exceeded():
                    break

                # Interleave targets so one expensive violation cannot consume
                # the entire bounded candidate budget before the others are
                # examined. This matters for long, multi-month blocks.
                accepted_weekend_swap = False
                maximum_rank = max(
                    (len(rows) for rows in ordered_rights.values()),
                    default=0,
                )
                for rank in range(maximum_rank):
                    if accepted_weekend_swap or runtime_exceeded():
                        break
                    for source_key in ordered_rights:
                        rights = ordered_rights[source_key]
                        if rank >= len(rights):
                            continue
                        if weekend_repairs_attempted >= weekend_attempt_limit:
                            break
                        right_pair = rights[rank]
                        left_pair = (source_key[2], source_key[3])
                        weekend_repairs_attempted += 1
                        pairwise_candidates_considered += 1
                        pairwise_swaps_attempted += 1
                        final_plateau_repair_attempts += 1
                        phase_attempts['final_plateau_repair'] += 1
                        result = evaluate_plateau_pairwise_swap(
                            instances=instances,
                            physicians=physicians,
                            state=state,
                            instances_by_id=instances_by_id,
                            manual_pairs=manual_pairs,
                            locked_open_instance_ids=set(source_locked_open_ids),
                            targets=targets,
                            contract_by_physician=contract_by_physician,
                            requests_by_physician_date=requests_by_physician_date,
                            eligible_facilities_by_physician=(
                                eligible_facilities_by_physician
                            ),
                            minimum_rest_by_physician=minimum_rest_by_physician,
                            current_score=final_score,
                            left_instance_id=left_pair[0],
                            left_physician_id=left_pair[1],
                            right_instance_id=right_pair[0],
                            right_physician_id=right_pair[1],
                        )
                        if not result.get('legal') or not result.get('improving'):
                            continue
                        score_before_swap = final_score
                        state = result['state']
                        plateau_scoring = result['scoring']
                        final_score = result['scoring']['score']
                        improvement_count += 1
                        weekend_repairs_accepted += 1
                        final_plateau_repair_accepts += 1
                        pairwise_swaps_accepted += 1
                        phase_improvements['final_plateau_repair'] += 1
                        plateau_improved = True
                        accepted_weekend_swap = True
                        final_plateau_repair_reason = 'improved'
                        accepted_pairwise_swap_details.append({
                            'action': 'weekend_violation_swap',
                            'weekend_anchor_shift_instance_id': source_key[0],
                            'weekend_violation_type': source_key[1],
                            'left_shift_instance_id': left_pair[0],
                            'left_physician_id': left_pair[1],
                            'right_shift_instance_id': right_pair[0],
                            'right_physician_id': right_pair[1],
                            'score_before': float(score_before_swap),
                            'score_after': float(final_score),
                            'score_delta': float(result['score_delta']),
                        })
                        break
                if not accepted_weekend_swap:
                    break

        targeted_night_types = {
            'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT',
            'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK',
            'NIGHT_OVER_MAXIMUM',
        }
        if not runtime_exceeded():
            def current_night_target_pairs():
                current_targets = []
                maximum_targets = []
                includes_night_maximum = False
                current_night_report = _night_violation_report(
                    instances, physicians, state, contract_by_physician,
                )
                for violation in current_night_report['night_violations']:
                    if violation['violation_type'] not in targeted_night_types:
                        continue
                    if violation['violation_type'] == 'NIGHT_OVER_MAXIMUM':
                        includes_night_maximum = True
                    movable_pairs = _current_violation_assignment_pairs(
                        violation, state, manual_pairs,
                    )
                    current_targets.extend(movable_pairs)
                    if violation['violation_type'] == 'NIGHT_OVER_MAXIMUM':
                        maximum_targets.extend(movable_pairs)
                return (
                    list(dict.fromkeys(current_targets)),
                    includes_night_maximum,
                    list(dict.fromkeys(maximum_targets)),
                )

            targeted_pairs, includes_night_maximum, maximum_target_pairs = current_night_target_pairs()

            def current_night_counts():
                counts = defaultdict(int)
                for assigned_instance_id, assigned_physician_ids in state.items():
                    assigned_instance = instances_by_id.get(assigned_instance_id)
                    if assigned_instance is None or not assigned_instance.shift_template.night_shift:
                        continue
                    for assigned_physician_id in assigned_physician_ids:
                        counts[assigned_physician_id] += 1
                return counts

            repaired_night = False
            direct_attempts_for_plateau = 0
            for instance_id, from_physician_id in targeted_pairs:
                night_counts = current_night_counts()
                ordered_recipient_ids = sorted(
                    physician_ids,
                    key=lambda candidate_id: (night_counts[candidate_id], candidate_id),
                )
                for to_physician_id in ordered_recipient_ids:
                    if direct_attempts_for_plateau >= plateau_candidate_limit:
                        break
                    if runtime_exceeded():
                        mark_timeout('final_plateau_repair')
                        final_plateau_repair_reason = 'runtime_limit'
                        break
                    direct_attempts_for_plateau += 1
                    final_plateau_repair_attempts += 1
                    night_recovery_repairs_attempted += 1
                    phase_attempts['final_plateau_repair'] += 1
                    result = try_final_plateau_reassign(
                        instance_id, from_physician_id, to_physician_id,
                    )
                    if result is None:
                        continue
                    trial_scoring, trial_state = result
                    if is_optimization_improvement(trial_scoring):
                        state = trial_state
                        plateau_scoring = trial_scoring
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        final_plateau_repair_accepts += 1
                        night_recovery_repairs_accepted += 1
                        phase_improvements['final_plateau_repair'] += 1
                        plateau_improved = True
                        repaired_night = True
                        final_plateau_repair_reason = 'improved'
                if runtime_exceeded():
                    break
                if direct_attempts_for_plateau >= plateau_candidate_limit:
                    break

            def legal_swap_state(base_state, left_instance_id, left_physician_id,
                                 right_instance_id, right_physician_id):
                if (
                    left_instance_id == right_instance_id
                    or left_physician_id == right_physician_id
                    or (left_instance_id, left_physician_id) in manual_pairs
                    or (right_instance_id, right_physician_id) in manual_pairs
                    or left_instance_id in source_locked_open_ids
                    or right_instance_id in source_locked_open_ids
                    or right_physician_id in base_state[left_instance_id]
                    or left_physician_id in base_state[right_instance_id]
                ):
                    return None
                trial_state = _copy_state(base_state)
                _replace_in_state(
                    trial_state, left_instance_id, left_physician_id, right_physician_id,
                )
                _replace_in_state(
                    trial_state, right_instance_id, right_physician_id, left_physician_id,
                )
                for candidate_instance_id, candidate_physician_id in (
                    (left_instance_id, right_physician_id),
                    (right_instance_id, left_physician_id),
                ):
                    candidate_instance = instances_by_id[candidate_instance_id]
                    if candidate_instance.facility_id not in eligible_facilities_by_physician.get(
                        candidate_physician_id, set(),
                    ):
                        return None
                    if not _can_assign_in_state(
                        trial_state,
                        instances_by_id,
                        candidate_instance,
                        candidate_physician_id,
                        eligible_facilities_by_physician,
                        minimum_rest_by_physician,
                        exclude_instance_id=candidate_instance_id,
                    ):
                        return None
                return trial_state

            # Build several coordinated exchanges before invoking the expensive
            # complete score. This can cross the multi-move neighborhood that a
            # concentrated two-month schedule requires, while the full official
            # objective still decides whether the entire batch is retained.
            batch_round_limit = 4
            batch_swap_limit = 48
            for _batch_round in range(batch_round_limit):
                if runtime_exceeded():
                    break
                (
                    targeted_pairs,
                    includes_night_maximum,
                    maximum_target_pairs,
                ) = current_night_target_pairs()
                if not maximum_target_pairs:
                    break
                batch_state = _copy_state(state)
                batch_counts = current_night_counts()
                batch_swaps = []
                ordered_maximum_pairs = sorted(
                    maximum_target_pairs,
                    key=lambda pair: (-batch_counts[pair[1]], pair[1], pair[0]),
                )
                for night_instance_id, donor_id in ordered_maximum_pairs:
                    if len(batch_swaps) >= batch_swap_limit:
                        break
                    night_instance = instances_by_id[night_instance_id]
                    recipient_ids = sorted(
                        (
                            candidate_id for candidate_id in physician_ids
                            if candidate_id != donor_id
                            and batch_counts[candidate_id] + 1 < batch_counts[donor_id]
                        ),
                        key=lambda candidate_id: (batch_counts[candidate_id], candidate_id),
                    )
                    accepted_batch_swap = False
                    for recipient_id in recipient_ids:
                        recipient_day_pairs = sorted(
                            (
                                (candidate_instance_id, candidate_owner_id)
                                for candidate_instance_id, candidate_owner_id in _optimizer_pairs(batch_state, manual_pairs)
                                if candidate_owner_id == recipient_id
                                and not instances_by_id[candidate_instance_id].shift_template.night_shift
                                and candidate_instance_id not in source_locked_open_ids
                            ),
                            key=lambda pair: (
                                abs(_shift_hours(instances_by_id[pair[0]]) - _shift_hours(night_instance)),
                                abs((instances_by_id[pair[0]].date - night_instance.date).days),
                                pair[0],
                            ),
                        )
                        for day_instance_id, _owner_id in recipient_day_pairs:
                            trial_batch_state = legal_swap_state(
                                batch_state,
                                night_instance_id,
                                donor_id,
                                day_instance_id,
                                recipient_id,
                            )
                            if trial_batch_state is None:
                                continue
                            batch_state = trial_batch_state
                            batch_counts[donor_id] -= 1
                            batch_counts[recipient_id] += 1
                            batch_swaps.append((
                                night_instance_id, donor_id, day_instance_id, recipient_id,
                            ))
                            accepted_batch_swap = True
                            break
                        if accepted_batch_swap:
                            break
                if not batch_swaps:
                    break
                batch_scoring = _score_schedule(
                    instances,
                    physicians,
                    batch_state,
                    targets,
                    contract_by_physician,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                )
                if has_hard_invalids(batch_scoring) or not is_optimization_improvement(batch_scoring):
                    break
                score_before_batch = final_score
                state = batch_state
                plateau_scoring = batch_scoring
                final_score = batch_scoring['score']
                improvement_count += 1
                final_plateau_repair_accepts += len(batch_swaps)
                pairwise_swaps_accepted += len(batch_swaps)
                night_recovery_repairs_accepted += len(batch_swaps)
                phase_attempts['final_plateau_repair'] += len(batch_swaps)
                phase_improvements['final_plateau_repair'] += len(batch_swaps)
                plateau_improved = True
                repaired_night = True
                final_plateau_repair_reason = 'improved'
                accepted_pairwise_swap_details.append({
                    'action': 'night_day_redistribution_batch',
                    'swap_count': len(batch_swaps),
                    'score_before': float(score_before_batch),
                    'score_after': float(final_score),
                    'score_delta': float(final_score - score_before_batch),
                })

            # A concentrated night fill is often not repairable by a direct
            # reassignment because the low-night recipient already owns a day
            # shift that overlaps or violates rest.  Try the coordinated move
            # the scheduler actually needs: give that day shift to the night
            # donor while transferring the night to the low-night physician.
            # The official complete score remains the acceptance rule.
            redistribution_round_limit = 64
            redistribution_attempt_limit = 2000
            redistribution_rounds = 0
            redistribution_attempts = 0
            while (
                not runtime_exceeded()
                and redistribution_rounds < redistribution_round_limit
                and redistribution_attempts < redistribution_attempt_limit
            ):
                (
                    targeted_pairs,
                    includes_night_maximum,
                    maximum_target_pairs,
                ) = current_night_target_pairs()
                if not maximum_target_pairs:
                    break
                night_counts = current_night_counts()
                ordered_maximum_pairs = sorted(
                    maximum_target_pairs,
                    key=lambda pair: (-night_counts[pair[1]], pair[1], pair[0]),
                )
                accepted_redistribution = False
                for night_instance_id, donor_id in ordered_maximum_pairs:
                    night_instance = instances_by_id[night_instance_id]
                    recipient_ids = sorted(
                        (
                            candidate_id for candidate_id in physician_ids
                            if candidate_id != donor_id
                            and night_counts[candidate_id] < night_counts[donor_id]
                        ),
                        key=lambda candidate_id: (night_counts[candidate_id], candidate_id),
                    )
                    for recipient_id in recipient_ids:
                        recipient_day_pairs = sorted(
                            (
                                (candidate_instance_id, candidate_owner_id)
                                for candidate_instance_id, candidate_owner_id in _optimizer_pairs(state, manual_pairs)
                                if candidate_owner_id == recipient_id
                                and not instances_by_id[candidate_instance_id].shift_template.night_shift
                                and candidate_instance_id not in source_locked_open_ids
                            ),
                            key=lambda pair: (
                                abs(_shift_hours(instances_by_id[pair[0]]) - _shift_hours(night_instance)),
                                abs((instances_by_id[pair[0]].date - night_instance.date).days),
                                pair[0],
                            ),
                        )
                        for day_instance_id, _recipient_owner_id in recipient_day_pairs:
                            if runtime_exceeded() or redistribution_attempts >= redistribution_attempt_limit:
                                break
                            redistribution_attempts += 1
                            pairwise_candidates_considered += 1
                            pairwise_swaps_attempted += 1
                            final_plateau_repair_attempts += 1
                            night_recovery_repairs_attempted += 1
                            phase_attempts['final_plateau_repair'] += 1
                            result = evaluate_plateau_pairwise_swap(
                                instances=instances,
                                physicians=physicians,
                                state=state,
                                instances_by_id=instances_by_id,
                                manual_pairs=manual_pairs,
                                locked_open_instance_ids=set(source_locked_open_ids),
                                targets=targets,
                                contract_by_physician=contract_by_physician,
                                requests_by_physician_date=requests_by_physician_date,
                                eligible_facilities_by_physician=eligible_facilities_by_physician,
                                minimum_rest_by_physician=minimum_rest_by_physician,
                                current_score=final_score,
                                left_instance_id=night_instance_id,
                                left_physician_id=donor_id,
                                right_instance_id=day_instance_id,
                                right_physician_id=recipient_id,
                            )
                            if not result['legal'] or not result['improving']:
                                continue
                            score_before_swap = final_score
                            state = result['state']
                            plateau_scoring = result['scoring']
                            final_score = result['scoring']['score']
                            improvement_count += 1
                            final_plateau_repair_accepts += 1
                            pairwise_swaps_accepted += 1
                            night_recovery_repairs_accepted += 1
                            phase_improvements['final_plateau_repair'] += 1
                            plateau_improved = True
                            repaired_night = True
                            accepted_redistribution = True
                            final_plateau_repair_reason = 'improved'
                            accepted_pairwise_swap_details.append({
                                'action': 'night_day_redistribution',
                                'left_shift_instance_id': night_instance_id,
                                'left_physician_id': donor_id,
                                'right_shift_instance_id': day_instance_id,
                                'right_physician_id': recipient_id,
                                'score_before': float(score_before_swap),
                                'score_after': float(final_score),
                                'score_delta': float(result['score_delta']),
                            })
                            break
                        if accepted_redistribution or runtime_exceeded():
                            break
                    if accepted_redistribution or runtime_exceeded():
                        break
                if not accepted_redistribution:
                    break
                redistribution_rounds += 1

            if not runtime_exceeded():
                pairwise_attempt_limit = min(
                    10000, max(4000, plateau_candidate_limit * 50),
                )
                pairwise_round_limit = 8
                accepted_swap_count_at_start = pairwise_swaps_accepted
                while pairwise_rescan_rounds < pairwise_round_limit:
                    if runtime_exceeded():
                        mark_timeout('final_plateau_repair')
                        final_plateau_repair_reason = 'runtime_limit'
                        break
                    (
                        targeted_pairs,
                        includes_night_maximum,
                        maximum_target_pairs,
                    ) = current_night_target_pairs()
                    workload_rows = plateau_scoring.get('workload_score_rows', [])
                    violating_workload_ids = {
                        row['physician_id'] for row in workload_rows
                        if row.get('deviation_direction') in {'above_maximum', 'below_minimum'}
                    }
                    movable_pairs = _optimizer_pairs(state, manual_pairs)
                    workload_pairs = [
                        pair for pair in movable_pairs
                        if pair[1] in violating_workload_ids
                    ]
                    violation_pairs = list(dict.fromkeys([*targeted_pairs, *workload_pairs]))
                    if not violation_pairs:
                        break
                    pairwise_rescan_rounds += 1
                    violation_pair_set = set(violation_pairs)
                    involved_physician_ids = {pair[1] for pair in violation_pairs}
                    nearby_pairs = [
                        pair for pair in movable_pairs
                        if pair[1] in involved_physician_ids and pair not in violation_pair_set
                    ]
                    nearby_pair_set = set(nearby_pairs)
                    other_pairs = [
                        pair for pair in movable_pairs
                        if pair not in violation_pair_set and pair not in nearby_pair_set
                    ]
                    night_counts = current_night_counts()
                    other_pairs.sort(key=lambda pair: (
                        night_counts[pair[1]],
                        0 if not instances_by_id[pair[0]].shift_template.night_shift else 1,
                        pair[1],
                        instances_by_id[pair[0]].date,
                        pair[0],
                    ))
                    # A normal one-month build is small enough to cover the same
                    # violation-involved neighborhood as explain_optimizer_plateau.
                    # Larger schedules stay focused on violating physicians.
                    prioritized_right_pairs = (
                        [*other_pairs, *nearby_pairs, *violation_pairs]
                        if includes_night_maximum
                        else [*violation_pairs, *nearby_pairs]
                    )
                    if len(movable_pairs) <= 250 and not includes_night_maximum:
                        prioritized_right_pairs.extend(other_pairs)
                    prioritized_right_pairs = list(dict.fromkeys(prioritized_right_pairs))
                    candidate_count = len(violation_pairs) * len(prioritized_right_pairs)
                    remaining_budget = pairwise_attempt_limit - pairwise_candidates_considered
                    if remaining_budget <= 0:
                        pairwise_candidates_skipped_by_cap += candidate_count
                        pairwise_best_missed_candidate_if_any = {
                            'reason': 'not_evaluated_due_to_attempt_cap',
                            'remaining_candidate_count': candidate_count,
                        }
                        break
                    round_candidates = _bounded_pairwise_candidates(
                        violation_pairs, prioritized_right_pairs, remaining_budget,
                    )
                    skipped_this_round = max(0, candidate_count - remaining_budget)
                    accepted_swap = False
                    night_target_pair_set = set(targeted_pairs)
                    for (
                        left_instance_id, left_physician_id,
                        right_instance_id, right_physician_id,
                    ) in round_candidates:
                        if runtime_exceeded():
                            mark_timeout('final_plateau_repair')
                            final_plateau_repair_reason = 'runtime_limit'
                            break
                        pairwise_candidates_considered += 1
                        pairwise_swaps_attempted += 1
                        final_plateau_repair_attempts += 1
                        if (left_instance_id, left_physician_id) in night_target_pair_set:
                            night_recovery_repairs_attempted += 1
                        phase_attempts['final_plateau_repair'] += 1
                        result = evaluate_plateau_pairwise_swap(
                            instances=instances,
                            physicians=physicians,
                            state=state,
                            instances_by_id=instances_by_id,
                            manual_pairs=manual_pairs,
                            locked_open_instance_ids=set(source_locked_open_ids),
                            targets=targets,
                            contract_by_physician=contract_by_physician,
                            requests_by_physician_date=requests_by_physician_date,
                            eligible_facilities_by_physician=eligible_facilities_by_physician,
                            minimum_rest_by_physician=minimum_rest_by_physician,
                            current_score=final_score,
                            left_instance_id=left_instance_id,
                            left_physician_id=left_physician_id,
                            right_instance_id=right_instance_id,
                            right_physician_id=right_physician_id,
                        )
                        if not result['legal']:
                            continue
                        delta = result['score_delta']
                        if best_pairwise_swap_delta is None or delta < best_pairwise_swap_delta:
                            best_pairwise_swap_delta = delta
                        if result['improving']:
                            score_before_swap = final_score
                            state = result['state']
                            plateau_scoring = result['scoring']
                            final_score = result['scoring']['score']
                            improvement_count += 1
                            final_plateau_repair_accepts += 1
                            pairwise_swaps_accepted += 1
                            if (left_instance_id, left_physician_id) in night_target_pair_set:
                                night_recovery_repairs_accepted += 1
                            phase_improvements['final_plateau_repair'] += 1
                            plateau_improved = True
                            repaired_night = True
                            accepted_swap = True
                            accepted_pairwise_swap_details.append({
                                'left_shift_instance_id': left_instance_id,
                                'left_physician_id': left_physician_id,
                                'right_shift_instance_id': right_instance_id,
                                'right_physician_id': right_physician_id,
                                'score_before': float(score_before_swap),
                                'score_after': float(final_score),
                                'score_delta': float(delta),
                            })
                            final_plateau_repair_reason = 'improved'
                            break
                    if accepted_swap:
                        if pairwise_rescan_rounds > 1:
                            pairwise_accepts_after_rescan += 1
                        continue
                    if skipped_this_round:
                        pairwise_candidates_skipped_by_cap += skipped_this_round
                        left_index, right_index = divmod(
                            remaining_budget, len(prioritized_right_pairs),
                        )
                        first_skipped = (
                            *violation_pairs[left_index], *prioritized_right_pairs[right_index],
                        )
                        pairwise_best_missed_candidate_if_any = {
                            'reason': 'not_evaluated_due_to_attempt_cap',
                            'remaining_candidate_count': skipped_this_round,
                            'first_skipped_candidate': {
                                'left_shift_instance_id': first_skipped[0],
                                'left_physician_id': first_skipped[1],
                                'right_shift_instance_id': first_skipped[2],
                                'right_physician_id': first_skipped[3],
                            },
                        }
                    break
                if (
                    pairwise_rescan_rounds >= pairwise_round_limit
                    and pairwise_swaps_accepted > accepted_swap_count_at_start
                ):
                    pairwise_best_missed_candidate_if_any = (
                        pairwise_best_missed_candidate_if_any or {
                            'reason': 'not_evaluated_due_to_round_cap',
                        }
                    )

        if not plateau_improved and final_plateau_repair_reason != 'runtime_limit':
            final_plateau_repair_reason = (
                'no_target_violations'
                if (
                    not violating_workload_ids
                    and not targeted_pairs
                    and not weekend_had_targets
                )
                else 'no_legal_improving_move_or_swap'
            )
        score_after_final_plateau_repair = float(final_score)

        final_scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
        )
        final_score = final_scoring['score']
        source_unfilled_shift_count = _unfilled_slot_count(instances, source_state_snapshot)
        coverage_improved_to_complete = bool(
            start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
            and source_unfilled_shift_count > 0
            and _result_priority(final_scoring, _unfilled_slot_count(instances, state))[0] == 0
        )
        if (
            start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
            and source_run is not None
            and _result_priority(source_state_reported_scoring, source_unfilled_shift_count)
            < _result_priority(final_scoring, _unfilled_slot_count(instances, state))
        ):
            state = _copy_state(source_state_snapshot)
            final_scoring = source_state_reported_scoring
            final_score = source_state_reported_score_before_pre_score_changes
            source_state_restored_as_best = True
        if adaptive_runtime:
            observe_best(state, final_scoring)
            if search_budget.best_score is not None:
                adaptive_continuation_started = True
                state, final_scoring, adaptive_debug = _run_adaptive_search_rounds(
                    instances=instances, physicians=physicians,
                    initial_state=adaptive_best_state,
                    initial_scoring=adaptive_best_scoring,
                    manual_pairs=manual_pairs, targets=targets,
                    contract_by_physician=contract_by_physician,
                    requests_by_physician_date=requests_by_physician_date,
                    eligible_facilities_by_physician=eligible_facilities_by_physician,
                    minimum_rest_by_physician=minimum_rest_by_physician,
                    search_budget=search_budget, rng=rng, debug=adaptive_debug,
                    progress_callback=progress_callback,
                )
                adaptive_best_state, adaptive_best_scoring = state, final_scoring
                final_score = final_scoring['score']
                improvement_count += adaptive_debug['improvements']
                candidates_considered_before_timeout += adaptive_debug['attempts']
                source_state_restored_as_best = False
                timed_out = False
            stopped_reason = (
                'productivity_exhausted'
                if adaptive_debug.get('productivity_exhausted')
                else 'restart_exhausted'
                if adaptive_debug.get('restart_exhausted')
                else search_budget.reason() or 'no_complete_valid_schedule'
            )
            adaptive_debug['stopped_reason'] = stopped_reason
            adaptive_debug['runtime_seconds'] = runtime_seconds_elapsed()
            adaptive_debug['seconds_since_improvement'] = monotonic() - search_budget.last_improvement

        final_fixed_request_diagnostic = _fixed_shift_on_request_workload_floor(
            instances,
            physicians,
            targets,
            requests_by_physician_date,
            eligible_facilities_by_physician,
            state=state,
            manual_assignment_only_physician_ids=manual_assignment_only_physician_ids,
        )
        adaptive_debug['reached_proven_score_floor'] = bool(
            final_fixed_request_diagnostic['all_resolved_requests_satisfied']
            and final_score <= proven_score_floor
        )

        search_finished_at = monotonic()
        final_workload_range_counts = _workload_range_counts(final_scoring)
        same_shift_violations_final = len(final_scoring['same_shift_violations'])
        final_night_report = _night_violation_report(
            instances,
            scoring_physicians,
            state,
            scoring_contract_by_physician,
        )
        final_night_block_debug = _night_block_debug(
            instances,
            physicians,
            state,
            contract_by_physician,
        )
        final_night_minimum_status = _night_minimum_status(
            instances,
            physicians,
            state,
            contract_by_physician,
        )
        night_minimum_unresolved_reasons = [
            {
                **row,
                'reason': (
                    'No improving valid optimizer reassignment or swap was found for this physician and period.'
                ),
            }
            for row in final_night_minimum_status['physicians_under_night_minimum']
        ]

        assignment_pairs_at_final_scoring = {
            (instance_id, physician_id)
            for instance_id, physician_ids in state.items()
            for physician_id in physician_ids
        }
        locked_open_instance_ids = {instance.id for instance in instances if instance.is_locked_open}
        for instance_id in locked_open_instance_ids:
            state[instance_id] = []
        assignment_pairs_after_locked_open_projection = {
            (instance_id, physician_id)
            for instance_id, physician_ids in state.items()
            for physician_id in physician_ids
        }
        assignments_removed_after_final_scoring = len(
            assignment_pairs_at_final_scoring - assignment_pairs_after_locked_open_projection
        )
        unlocked_manual_ids = [
            assignment.id for assignment in assignments
            if assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and not assignment.is_locked
            and assignment.physician_id not in manual_assignment_only_physician_ids
        ]
        if unlocked_manual_ids:
            ScheduleShiftAssignment.objects.filter(id__in=unlocked_manual_ids).delete()

        invalid_assignment_capacity = _invalid_state_assignment_capacity(
            instances, state, manual_assignment_only_physician_ids,
        )
        if invalid_assignment_capacity:
            raise ValueError(
                'Optimizer produced duplicate or over-capacity assignments; no run assignments were persisted. '
                f'Details: {invalid_assignment_capacity}'
            )

        persistence_started_at = monotonic()
        assignment_rows = []
        changed_instances = []
        persisted_at = timezone.now()
        for instance in instances:
            optimizer_physician_ids = [
                physician_id
                for physician_id in state[instance.id]
                if (instance.id, physician_id) not in manual_pairs
            ]
            for physician_id in optimizer_physician_ids:
                assignment_rows.append(ScheduleShiftAssignment(
                    shift_instance=instance,
                    physician_id=physician_id,
                    created_by=created_by,
                    assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
                    optimizer_run=optimizer_run,
                ))

            next_status = (
                ScheduleShiftInstance.Status.ASSIGNED
                if len(state[instance.id]) >= instance.required_staffing
                else ScheduleShiftInstance.Status.OPEN
            )
            if instance.status != next_status:
                instance.status = next_status
                instance.updated_at = persisted_at
                changed_instances.append(instance)

        # Keep the snapshot atomic without one database round trip per slot.
        ScheduleShiftAssignment.objects.bulk_create(assignment_rows, batch_size=1000)
        ScheduleShiftInstance.objects.bulk_update(changed_instances, ['status', 'updated_at'], batch_size=500)
        persistence_finished_at = monotonic()

        persisted_visible_assignment_pairs = set(
            assignments_for_viewed_run(version, optimizer_run).values_list(
                'shift_instance_id', 'physician_id'
            )
        )
        visible_assignments_added_after_final_scoring = len(
            persisted_visible_assignment_pairs - assignment_pairs_after_locked_open_projection
        )
        visible_assignments_missing_after_final_scoring = len(
            assignment_pairs_after_locked_open_projection - persisted_visible_assignment_pairs
        )

        unfilled_shift_count = _unfilled_slot_count(instances, state)
        assignments_made_during_search = assignments_made
        assignments_made = len(persisted_visible_assignment_pairs - loaded_start_assignment_pairs)
        improvements_during_search = improvement_count
        if source_state_restored_as_best:
            improvement_count = 0

    preserve_timeout_result = _should_preserve_timeout_result(
        timed_out=timed_out,
        initial_score=reported_initial_score,
        final_score=final_score,
        final_scoring=final_scoring,
        unfilled_shift_count=unfilled_shift_count,
        coverage_improved_to_complete=coverage_improved_to_complete,
    )
    # Reaching a configured search boundary is a normal optimizer completion,
    # not an execution failure.  A non-improving bounded result remains saved
    # for comparison while the previous active run stays selected.
    activate_result = not timed_out or preserve_timeout_result

    if preserve_timeout_result:
        message = 'Optimizer reached the runtime limit. Best complete improved schedule retained.'
    elif timed_out:
        message = (
            'Optimizer completed at the search limit without finding a better schedule. '
            'This run was saved for comparison; the previous active run was preserved.'
        )
    elif source_state_restored_as_best:
        message = 'Previous schedule retained; no better complete/valid result was found.'
    elif coverage_improved_to_complete:
        message = (
            'Complete valid coverage retained in preference to the incomplete starting schedule. '
            'The total penalty may be higher; contract weights are unchanged.'
        )
    elif open_slots_available == 0:
        message = 'No open slots were available; manual assignments were preserved.'
    elif assignments_made == 0:
        message = 'Optimizer completed, but no eligible open slots could be assigned.'
    elif improvement_count == 0:
        message = (
            f'Optimizer completed: {assignments_made} assignment(s) made, '
            f'{unfilled_shift_count} unfilled shift(s). No improving moves found.'
        )
    else:
        message = (
            f'Optimizer completed: {assignments_made} assignment(s) made, '
            f'{unfilled_shift_count} unfilled shift(s), '
            f'{improvement_count} improvement(s) kept.'
        )

    final_breakdown = {
        key: float(value)
        for key, value in final_scoring['breakdown'].items()
    }
    if adaptive_runtime:
        total_minutes = search_budget.total_seconds / 60
        total_minutes_label = int(total_minutes) if total_minutes.is_integer() else total_minutes
        stop_label = {'stall_limit': f'{search_budget.stall_seconds:g} seconds without a new best schedule',
                      'overall_runtime_limit': f'the {total_minutes_label}-minute limit',
                      'productivity_exhausted': 'several independently seeded search portfolios were exhausted without improvement',
                      'restart_exhausted': 'three diversified search windows without improvement',
                      'user_stop': 'your Stop request', 'score_zero': 'score zero',
                      'proven_score_floor': 'the proven request-compliant score floor'}.get(stopped_reason, stopped_reason)
        if activate_result:
            message = f'Optimizer finished after {stop_label}. Best complete valid schedule retained.'
        else:
            message = (
                f'Optimizer finished after {stop_label} without finding a better schedule. '
                'This run was saved for comparison; the previous active run was preserved.'
            )
    final_validation = final_scoring['validation']
    final_request_rows = _request_scoring_rows(
        instances,
        scoring_physicians,
        state,
        scoring_contract_by_physician,
        requests_by_physician_date,
    )
    final_score_audit = _score_audit(
        final_scoring,
        final_night_report,
        final_request_rows,
    )
    runtime_seconds = (
        runtime_seconds_elapsed()
    )

    summary = {
        'message': message,
        'optimizer_run_id': optimizer_run.id,
        'optimizer_run_number': optimizer_run.run_number,
        'start_mode': start_mode,
        'seed': seed,
        'total_score': float(final_score),
        'initial_score': float(reported_initial_score),
        'final_score': float(final_score),
        'timed_out': timed_out,
        'partial_result_preserved': preserve_timeout_result,
        'stopped_reason': stopped_reason,
        'runtime_seconds': runtime_seconds,
        'score_breakdown': final_breakdown,
        'same_shift_violations_count': same_shift_violations_final,
        'night_violations_count': final_night_report['night_violations_count'],
        'total_night_shifts': final_night_report['total_night_shifts'],
        'max_nights_assigned_to_one_physician': final_night_report['max_nights_assigned_to_one_physician'],
        'night_fix_improvements': night_fix_improvements,
        'night_unresolved_reasons': final_night_report['night_unresolved_reasons'],
        **final_validation,
        'improvement_count': improvement_count,
        'iterations_run': iterations_run,
        'unfilled_shift_count': unfilled_shift_count,
        'assignments_made': assignments_made,
        'request_violations_summary': {
            'violations': final_scoring['request_violations'],
            'rewards': final_scoring['request_rewards'],
        },
        'fixed_request_feasibility': final_fixed_request_diagnostic,
        'candidate_rest_rejections': rest_violations_blocked,
        'rest_violations_blocked': rest_violations_blocked,
            'debug': {
            'seed': seed,
            'start_mode': start_mode,
            'source_optimizer_run_id': source_run.id if source_run is not None else None,
            'source_assignment_count': source_assignment_count,
            'source_assignment_count_raw': source_assignment_count_raw,
            'source_assignment_normalization': source_assignment_normalization,
            'manual_only_optimizer_source_rows_dropped': (
                manual_only_optimizer_source_rows_dropped
            ),
            'manual_only_request_assignments_seeded': (
                manual_only_request_assignments_seeded
            ),
            'manual_only_request_assignments_already_present': (
                manual_only_request_assignments_already_present
            ),
            'manual_only_request_optimizer_owners_displaced': (
                manual_only_request_optimizer_owners_displaced
            ),
            'manual_only_request_conflicts': manual_only_request_conflicts,
            'seeded_assignment_count': len(assignments),
            'source_unique_assignment_count': len(source_visible_assignment_pairs),
            'copied_start_assignment_count': len(loaded_start_assignment_pairs),
            'assignments_same_at_start': assignments_same_at_start,
            'source_pairs_missing_at_start': source_pairs_missing_at_start,
            'source_pairs_extra_at_start': source_pairs_extra_at_start,
            'source_state_score_before_pre_score_changes': float(
                source_state_score_before_pre_score_changes
            ),
            'source_state_reported_score_before_pre_score_changes': float(
                source_state_reported_score_before_pre_score_changes
            ),
            'source_state_restored_as_best': source_state_restored_as_best,
            'adaptive_runtime': adaptive_debug,
            'fixed_request_feasibility': final_fixed_request_diagnostic,
            'proven_score_floor': float(proven_score_floor),
            'reached_proven_score_floor': adaptive_debug['reached_proven_score_floor'],
            'coverage_improved_to_complete': coverage_improved_to_complete,
            'source_unfilled_shift_count': source_unfilled_shift_count,
            'assignments_made_during_search': assignments_made_during_search,
            'improvements_during_search': improvements_during_search,
            'initial_score_includes_internal_night_heuristics': False,
            'final_score_includes_internal_night_heuristics': False,
            'assignments_added_before_first_score': len(
                assignment_pairs_added_before_first_score
            ),
            'assignments_removed_before_first_score': len(
                assignment_pairs_removed_before_first_score
            ),
            'assignments_changed_before_first_score': len(
                assignment_pairs_added_before_first_score
                | assignment_pairs_removed_before_first_score
            ),
            'locked_open_shift_instance_count': len(source_locked_open_ids),
            'assignments_removed_after_final_scoring': assignments_removed_after_final_scoring,
            'persisted_visible_assignment_count': len(persisted_visible_assignment_pairs),
            'visible_assignments_added_after_final_scoring': (
                visible_assignments_added_after_final_scoring
            ),
            'visible_assignments_missing_after_final_scoring': (
                visible_assignments_missing_after_final_scoring
            ),
            'schedule_version_id': version.id,
            'schedule_block_id': version.schedule_block_id,
            'schedule_block_start_date': version.schedule_block.start_date.isoformat(),
            'schedule_block_end_date': version.schedule_block.end_date.isoformat(),
            'shift_instances_considered': len(instances),
            'assignment_rows_before': assignment_rows_before,
            'optimizer_assignments_deleted': optimizer_assignments_deleted,
            'manual_assignments_preserved': manual_assignments_preserved,
            'open_slots_considered': open_slots_available,
            'assignments_created': assignments_made,
            'initial_score': float(reported_initial_score),
            'search_initial_score': float(search_initial_score),
            'final_score': float(final_score),
            'initial_score_breakdown': {
                key: float(value)
                for key, value in initial_scoring['breakdown'].items()
            },
            'final_score_breakdown': final_breakdown,
            'score_audit': final_score_audit,
            'score_audit_warnings': final_score_audit['warnings'],
            'initial_fill_workload_guard_enabled': True,
            'initial_fill_workload_guard_candidates_above_max': initial_fill_workload_guard_candidates_above_max,
            'initial_fill_workload_guard_candidates_deprioritized': initial_fill_workload_guard_candidates_deprioritized,
            'initial_fill_workload_guard_examples': initial_fill_workload_guard_examples,
            'phase_order': phase_order,
            'phase_passes_run': phase_passes_run,
            'phase_attempts': {phase: phase_attempts.get(phase, 0) for phase in phase_order},
            'phase_improvements': {phase: phase_improvements.get(phase, 0) for phase in phase_order},
            'request_repair_attempts': request_repair_attempts,
            'request_repair_improvements': request_repair_improvements,
            'night_minimum_repair_attempts': phase_attempts.get('night_minimum_repair', 0),
            'night_minimum_repair_improvements': phase_improvements.get('night_minimum_repair', 0),
            'post_night_recovery_repair_attempts': post_night_recovery_repair_attempts,
            'post_night_recovery_repair_improvements': post_night_recovery_repair_improvements,
            'workload_repair_attempts': workload_repair_attempts,
            'workload_repair_improvements': workload_repair_improvements,
            'workload_over_range_count_initial': initial_workload_range_counts['over'],
            'workload_under_range_count_initial': initial_workload_range_counts['under'],
            'workload_over_range_count_final': final_workload_range_counts['over'],
            'workload_under_range_count_final': final_workload_range_counts['under'],
            'workload_candidate_moves_considered': workload_candidate_moves_considered,
            'reassignment_candidates_rejected_incrementally': (
                reassignment_candidates_rejected_incrementally
            ),
            'reassignment_candidates_full_score_verified': (
                reassignment_candidates_full_score_verified
            ),
            'workload_candidate_swaps_considered': workload_candidate_swaps_considered,
            'workload_moves_accepted': workload_moves_accepted,
            'workload_revisit': workload_revisit_debug,
            'night_spacing_revisit': night_spacing_debug,
            'bounded_phase_runtime_seconds': {
                **dict(phase_runtime_seconds),
                'workload_revisit': workload_revisit_debug.get('runtime_seconds', 0),
                'night_spacing_revisit': night_spacing_debug.get('runtime_seconds', 0),
            },
            'workload_swaps_accepted': workload_swaps_accepted,
            'workload_minimum_repair_physicians_considered': workload_minimum_repair_physicians_considered,
            'workload_minimum_repair_candidates_considered': workload_minimum_repair_candidates_considered,
            'workload_minimum_repair_accepts': workload_minimum_repair_accepts,
            'workload_minimum_repair_best_delta': (
                float(workload_minimum_repair_best_delta)
                if workload_minimum_repair_best_delta is not None else None
            ),
            'workload_minimum_repair_rejected_reasons': dict(
                workload_minimum_repair_rejected_reasons
            ),
            'workload_minimum_repair_turner_summary': {
                **workload_minimum_repair_turner_summary,
                'rejected_reasons': dict(
                    workload_minimum_repair_turner_summary['rejected_reasons']
                ),
                'attempt_guard_reached': (
                    workload_minimum_repair_candidates_considered
                    >= minimum_repair_attempt_limit
                ),
                'search_exhausted_without_accept': minimum_repair_exhausted,
            },
            'workload_score_initial': float(initial_scoring['breakdown']['workload_score']),
            'workload_score_final': float(final_scoring['breakdown']['workload_score']),
            'final_plateau_repair_attempts': final_plateau_repair_attempts,
            'final_plateau_repair_accepts': final_plateau_repair_accepts,
            'workload_micro_repairs_attempted': workload_micro_repairs_attempted,
            'workload_micro_repairs_accepted': workload_micro_repairs_accepted,
            'workload_micro_repair_best_delta': (
                float(workload_micro_repair_best_delta)
                if workload_micro_repair_best_delta is not None else None
            ),
            'workload_micro_repair_best_rejected_reason': workload_micro_repair_best_rejected_reason,
            'workload_micro_repair_accepted_details': workload_micro_repair_accepted_details,
            'over_max_physicians_considered': over_max_physicians_considered,
            'night_recovery_repairs_attempted': night_recovery_repairs_attempted,
            'night_recovery_repairs_accepted': night_recovery_repairs_accepted,
            'pairwise_swaps_attempted': pairwise_swaps_attempted,
            'pairwise_swaps_accepted': pairwise_swaps_accepted,
            'best_pairwise_swap_delta': (
                float(best_pairwise_swap_delta)
                if best_pairwise_swap_delta is not None else None
            ),
            'accepted_pairwise_swap_details': accepted_pairwise_swap_details,
            'pairwise_rescan_rounds': pairwise_rescan_rounds,
            'pairwise_candidates_considered': pairwise_candidates_considered,
            'pairwise_candidates_skipped_by_cap': pairwise_candidates_skipped_by_cap,
            'pairwise_best_missed_candidate_if_any': pairwise_best_missed_candidate_if_any,
            'pairwise_accepts_after_rescan': pairwise_accepts_after_rescan,
            'weekend_repairs_attempted': weekend_repairs_attempted,
            'weekend_repairs_accepted': weekend_repairs_accepted,
            'weekend_repair_rescan_rounds': weekend_repair_rescan_rounds,
            'score_before_final_plateau_repair': score_before_final_plateau_repair,
            'score_after_final_plateau_repair': score_after_final_plateau_repair,
            'final_plateau_repair_reason': final_plateau_repair_reason,
            'general_swap_attempts': general_swap_attempts,
            'general_swap_improvements': general_swap_improvements,
            'stopped_reason': stopped_reason,
            'runtime_seconds': runtime_seconds,
            'timed_out': timed_out,
            'phase_running_when_stopped': phase_running_when_stopped,
            'candidates_considered_before_timeout': candidates_considered_before_timeout,
            'full_score_evaluations': _FULL_SCORE_EVALUATIONS.get(),
            'score_cache': {
                'enabled': True,
                'max_entries': SCORE_CACHE_MAX_ENTRIES,
                'entries': len(_SCORE_CACHE.get() or ()),
                'hits': _SCORE_CACHE_HITS.get(),
                'misses': _SCORE_CACHE_MISSES.get(),
                'evictions': _SCORE_CACHE_EVICTIONS.get(),
            },
            'phase_timings_seconds': {
                'setup': optimizer_search_started_at - invocation_started_at,
                'search_and_scoring': search_finished_at - optimizer_search_started_at,
                'final_diagnostics': persistence_started_at - search_finished_at,
                'assignment_persistence': persistence_finished_at - persistence_started_at,
            },
            'partial_result_preserved': preserve_timeout_result,
            **final_validation,
            'improvement_count': improvement_count,
            'iterations_run': iterations_run,
            'workload_transfer_attempts': workload_transfer_attempts,
            'workload_transfer_improvements': workload_transfer_improvements,
            'same_shift_break_attempts': same_shift_break_attempts,
            'same_shift_break_improvements': same_shift_break_improvements,
            'same_shift_violations_initial': same_shift_violations_initial,
            'same_shift_violations_final': same_shift_violations_final,
            'same_shift_violations_count': same_shift_violations_final,
            'same_shift_fix_attempts': same_shift_break_attempts,
            'same_shift_fix_valid_alternatives': same_shift_fix_valid_alternatives,
            'same_shift_fix_improvements': same_shift_break_improvements,
            'same_shift_violations': final_scoring['same_shift_violations'],
            'total_night_shifts': final_night_report['total_night_shifts'],
            'night_shifts_by_physician': final_night_report['night_shifts_by_physician'],
            'night_violations_count': final_night_report['night_violations_count'],
            'night_violations': final_night_report['night_violations'],
            'night_unresolved_reasons': final_night_report['night_unresolved_reasons'],
            'contracts_used_by_physician': final_night_report['contracts_used_by_physician'],
            'night_rules_by_contract': final_night_report['night_rules_by_contract'],
            'physicians_without_contract': final_night_report['physicians_without_contract'],
            'night_block_candidates_by_physician': final_night_report['night_block_candidates_by_physician'],
            'night_minimum_violations_by_contract': final_night_report['night_minimum_violations_by_contract'],
            'night_maximum_violations_by_contract': final_night_report['night_maximum_violations_by_contract'],
            'min_consecutive_night_violations': final_night_report['min_consecutive_night_violations'],
            'max_consecutive_night_violations': final_night_report['max_consecutive_night_violations'],
            'post_night_to_non_night_recovery_violations': final_night_report['post_night_to_non_night_recovery_violations'],
            'post_night_to_next_night_block_recovery_violations': final_night_report['post_night_to_next_night_block_recovery_violations'],
            'night_score_initial': float(initial_night_report['score']),
            'night_score_final': float(final_night_report['score']),
            'night_block_debug_initial': initial_night_block_debug,
            **final_night_block_debug,
            'night_block_builder_enabled': NIGHT_BLOCK_BUILDER_ENABLED,
            'night_construction_heuristics_enabled': NIGHT_CONSTRUCTION_HEURISTICS_ENABLED,
            'night_block_builder_skipped': not NIGHT_BLOCK_BUILDER_ENABLED,
            'night_block_builder_disabled_reason': (
                None
                if NIGHT_BLOCK_BUILDER_ENABLED
                else NIGHT_BLOCK_BUILDER_DISABLED_REASON
            ),
            'night_shift_instances_considered': len(night_instances),
            'night_block_candidates_created': night_block_builder_candidates_created,
            'night_block_builder_blocks_assigned': night_block_builder_assigned_blocks,
            'night_block_lengths_assigned': night_block_builder_lengths_assigned,
            'night_block_assignment_rejections_by_reason': dict(night_block_builder_rejections_by_reason),
            'physicians_below_night_min_before_night_build': physicians_below_night_min_before_night_build,
            'physicians_below_night_min_after_night_build': physicians_below_night_min_after_night_build,
            'night_recovery_conflicts_after_night_build': night_recovery_conflicts_after_night_build,
            'night_distribution_by_physician': night_distribution_by_physician_after_build,
            'night_block_builder_score_before': night_block_builder_score_before,
            'night_block_builder_score_after': night_block_builder_score_after,
            'night_minimum_required': final_night_minimum_status['night_minimum_required'],
            'night_minimum_period': final_night_minimum_status['night_minimum_period'],
            'eligible_physicians_for_nights': len(physicians),
            'physicians_under_night_minimum': final_night_minimum_status['physicians_under_night_minimum'],
            'night_minimum_violations_count': final_night_minimum_status['night_minimum_violations_count'],
            'night_minimum_rules_applied': final_night_minimum_status['night_minimum_rules_applied'],
            'night_minimum_rules_suppressed_as_duplicates': final_night_minimum_status['night_minimum_rules_suppressed_as_duplicates'],
            'night_minimum_fix_attempts': night_minimum_fix_attempts,
            'night_minimum_fix_valid_alternatives': night_minimum_fix_valid_alternatives,
            'night_minimum_fix_improvements': night_minimum_fix_improvements,
            'night_minimum_direct_reassignment_attempts': night_minimum_direct_reassignment_attempts,
            'night_minimum_direct_reassignment_successes': night_minimum_direct_reassignment_successes,
            'night_minimum_swap_attempts': night_minimum_swap_attempts,
            'night_minimum_swap_successes': night_minimum_swap_successes,
            'night_minimum_unresolved_reasons': night_minimum_unresolved_reasons,
            'night_minimum_repair_debug': night_minimum_repair_debug,
            'workload_score_total': float(final_scoring['breakdown']['workload_score']),
            'workload_score_rows': final_scoring['workload_score_rows'],
            'night_fix_attempts': night_fix_attempts,
            'night_fix_valid_alternatives': night_fix_valid_alternatives,
            'night_fix_improvements': night_fix_improvements,
            'night_block_assignment_attempts': night_block_assignment_attempts,
            'night_block_assignment_successes': night_block_assignment_successes,
            'nonnight_assignments_blocked_by_recovery': nonnight_assignments_blocked_by_recovery,
            'nonnight_assignments_allowed_despite_recovery': nonnight_assignments_allowed_despite_recovery,
            'max_nights_assigned_to_one_physician': final_night_report['max_nights_assigned_to_one_physician'],
            'consecutive_day_break_attempts': consecutive_day_break_attempts,
            'consecutive_day_break_improvements': consecutive_day_break_improvements,
            'swap_attempts': swap_attempts,
            'swap_improvements': swap_improvements,
            'reassignment_moves_attempted': (
                workload_transfer_attempts
                + same_shift_break_attempts
                + consecutive_day_break_attempts
            ),
            'reassignment_moves_improved': (
                workload_transfer_improvements
                + same_shift_break_improvements
                + consecutive_day_break_improvements
            ),
            'balancing_moves_attempted': workload_transfer_attempts,
            'balancing_moves_improved': workload_transfer_improvements,
            'swap_moves_attempted': swap_attempts,
            'swap_moves_improved': swap_improvements,
            'candidate_rest_rejections': rest_violations_blocked,
            'duplicate_shift_instance_keys': [
                {
                    'date': item['date'].isoformat(),
                    'shift_template_id': item['shift_template_id'],
                    'row_count': item['row_count'],
                }
                for item in duplicate_shift_instances
            ],
        },
        'workload_summary': _build_workload_summary(
            physicians,
            final_scoring['physician_hours'],
            final_scoring['physician_shifts'],
            final_scoring['physician_night_shifts'],
            targets,
            final_scoring['workload_score_rows'],
        ),
    }
    with transaction.atomic():
        if activate_result:
            OptimizerRun.objects.filter(schedule_version=version, is_active=True).exclude(id=optimizer_run.id).update(is_active=False)
        optimizer_run.status = OptimizerRun.Status.COMPLETED
        optimizer_run.initial_score = summary['initial_score']
        optimizer_run.final_score = summary['final_score']
        optimizer_run.score_breakdown = summary['score_breakdown']
        optimizer_run.optimizer_summary = summary
        optimizer_run.optimizer_debug = summary.get('debug', {})
        optimizer_run.score_is_stale = False
        optimizer_run.is_active = activate_result
        if preserve_timeout_result:
            optimizer_run.notes = 'Runtime limit reached; best complete improved schedule retained.'
        elif timed_out:
            optimizer_run.notes = (
                'Search limit reached without improvement; run saved for comparison. '
                'Previous active run preserved.'
            )
        optimizer_run.save(update_fields=[
            'status',
            'initial_score',
            'final_score',
            'score_breakdown',
            'optimizer_summary',
            'optimizer_debug',
            'seed',
            'is_active',
            'notes',
            'score_is_stale',
        ])
        if activate_result:
            version.optimizer_summary = summary
            version.score_is_stale = False
            version.save(update_fields=['optimizer_summary', 'score_is_stale', 'updated_at'])
    # Release cached scoring reports promptly in long-lived worker processes.
    # Diagnostics above retain the hit/miss totals needed for comparison.
    _SCORE_CACHE.set(None)
    return summary
