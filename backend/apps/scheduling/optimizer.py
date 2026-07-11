from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from datetime import timedelta
from math import ceil
import random
import secrets
from time import monotonic

from django.db import transaction
from django.db.models import Count, Q

from .models import (
    ContractUserAssignment,
    OptimizerRun,
    ScheduleBlock,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
)


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
MAX_RUNTIME_SECONDS = 60
NIGHT_BLOCK_BUILDER_ENABLED = False
NIGHT_BLOCK_BUILDER_DISABLED_REASON = 'Disabled after runtime regression'


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
                    DEFAULT_WORKLOAD_RULE_PENALTY,
                ),
                'max_penalty_weight': _positive_decimal_or_default(
                    rule.get('max_penalty_weight'),
                    DEFAULT_WORKLOAD_RULE_PENALTY,
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


def _request_weight(contract, weight):
    settings = contract.request_settings if isinstance(contract.request_settings, dict) else {}
    configured = _decimal_or_none(settings.get(f'weight_{weight.lower()}'))
    if configured is not None:
        return configured
    return Decimal(DEFAULT_REQUEST_WEIGHTS.get(weight, 30))


def _requests_for_shift(requests_by_physician_date, physician_id, instance):
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
    score = Decimal('0')
    request_violations = 0
    request_rewards = 0
    for schedule_request in schedule_requests:
        weight = _request_weight(contract, schedule_request.weight)
        if schedule_request.request_type in {
            ScheduleRequest.RequestType.DAY_OFF,
            ScheduleRequest.RequestType.SHIFT_OFF,
        }:
            score += weight
            request_violations += 1
        else:
            score -= weight / Decimal('2')
            request_rewards += 1
    return score, request_violations, request_rewards


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
                else:
                    rows.append(
                        _request_violation_row(
                            schedule_request,
                            physician,
                            -(weight / Decimal('2')),
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
                            _request_weight(contract, schedule_request.weight) / Decimal('2'),
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
                            _request_weight(contract, schedule_request.weight) / Decimal('2'),
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
                'effective_workload_range': (
                    {
                        'period_type': primary_rule['period_type'],
                        'period_start': primary_rule['period_start'],
                        'period_end': primary_rule['period_end'],
                        'units': primary_rule['units'],
                        'raw_min_value': primary_rule['raw_min_value'],
                        'raw_max_value': primary_rule['raw_max_value'],
                        'min_value': primary_rule['effective_min_value'],
                        'max_value': primary_rule['effective_max_value'],
                        'proration': primary_rule['proration'],
                        'debug_warning': primary_rule['debug_warning'],
                    }
                    if primary_rule
                    else None
                ),
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
        state[assignment.shift_instance_id].append(assignment.physician_id)
        if (
            assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
        ):
            manual_pairs.add((assignment.shift_instance_id, assignment.physician_id))
    return state, manual_pairs


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
    return intervals


def _can_assign_in_state(
    state,
    instances_by_id,
    instance,
    physician_id,
    eligible_facilities_by_physician,
    minimum_rest_by_physician,
    exclude_instance_id=None,
):
    if physician_id in state[instance.id] and exclude_instance_id != instance.id:
        return False
    if instance.facility_id not in eligible_facilities_by_physician.get(physician_id, set()):
        return False

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


def _validate_schedule(
    instances,
    physicians,
    state,
    eligible_facilities_by_physician,
    minimum_rest_by_physician,
):
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
            if instance.facility_id not in eligible_facilities_by_physician.get(physician_id, set()):
                facility_ineligible_violations += 1
            intervals_by_physician[physician_id].append(instance.id)

    overlap_violations = 0
    rest_violations = 0
    for physician_id, instance_ids in intervals_by_physician.items():
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
    return (
        _workload_rule_limit(
            contract,
            'max_same_shifts_in_row',
            DEFAULT_MAX_SAME_SHIFT_STREAK,
        ),
        _workload_rule_penalty(
            contract,
            'max_same_shifts_in_row_penalty_weight',
            DEFAULT_SAME_SHIFT_PENALTY,
        ),
    )


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
        ) or DEFAULT_MAX_CONSECUTIVE_NIGHTS,
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
        return Decimal(str(default))
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
    windows = []
    seen = set()
    for instance in instances:
        window_start, window_end = _period_contains_date(
            period_type,
            period_start,
            period_end,
            instance.date,
        )
        key = (window_start, window_end)
        if key not in seen:
            seen.add(key)
            windows.append(key)
    return sorted(windows)


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


def _night_minimum_rule_evaluation(instances, physicians, state, contract_by_physician):
    instances_by_id = {instance.id: instance for instance in instances}
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
                count = _night_count_for_window(
                    instances_by_id,
                    state,
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
    ) or DEFAULT_MAX_CONSECUTIVE_NIGHTS
    min_consecutive = _configured_positive_int(
        settings,
        'min_consecutive_night_shifts',
    )
    projected_blocks = _night_blocks([*physician_nights, instance])
    for block in projected_blocks:
        dates = {item.date for item in block}
        if instance.date not in dates:
            continue
        if len(block) > max_consecutive:
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
            return -max(
                DEFAULT_NIGHT_BLOCK_EXTENSION_BONUS,
                days_after_penalty * Decimal(min(days_after, max_consecutive)),
                min_consecutive_bonus,
            )
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
):
    physicians_by_id = {physician.id: physician for physician in physicians}
    instances_by_id = {instance.id: instance for instance in instances}
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
    default_target = (
        Decimal(total_night_shifts) / Decimal(len(physicians))
        if physicians
        else Decimal('0')
    )
    night_minimum_rules_applied = []
    night_minimum_rules_suppressed_as_duplicates = []

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
        night_blocks = _night_blocks(night_instances)

        configured_volume_rule = False
        minimum_evaluation = _night_minimum_rule_evaluation(
            instances,
            [physician],
            state,
            contract_by_physician,
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
                            'night_block_dates': [
                                _block_dates(block)
                                for block in night_blocks
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

        if not configured_volume_rule:
            excess = max(Decimal(night_count) - (default_target + Decimal('1')), Decimal('0'))
            if excess > 0:
                penalty = excess * excess * DEFAULT_NIGHT_BALANCE_PENALTY
                score += penalty
                violations.append(
                    {
                        'physician_id': physician_id,
                        'physician': _physician_display_name(physician),
                        'violation_type': 'NIGHT_CONCENTRATION',
                        'dates_involved': [instance.date.isoformat() for instance in night_instances],
                        'night_block_dates': [_block_dates(block) for block in night_blocks],
                        'configured_limit': float(default_target + Decimal('1')),
                        'actual_value': night_count,
                        'penalty_weight': float(DEFAULT_NIGHT_BALANCE_PENALTY),
                        'penalty': float(penalty),
                        'explanation': 'Night-shift load is above the v0 default distribution target.',
                    }
                )

        max_consecutive = _configured_positive_int(
            settings,
            'max_consecutive_night_shifts',
        ) or DEFAULT_MAX_CONSECUTIVE_NIGHTS
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
            if min_consecutive is not None and len(block) < min_consecutive:
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
            if len(block) > max_consecutive:
                excess = len(block) - max_consecutive
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
                and (min_consecutive or max_consecutive) > 1
                and (
                    block[0].date - timedelta(days=1) in assigned_night_dates
                    or block[0].date + timedelta(days=1) in assigned_night_dates
                )
            ):
                days_after_penalty = _configured_positive_penalty(
                    settings,
                    'days_off_after_night_block_penalty_weight',
                    DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
                )
                score += max(
                    DEFAULT_NIGHT_BLOCK_EXTENSION_BONUS,
                    days_after_penalty * Decimal('2'),
                )

        assignments = sorted(
            assignments_by_physician[physician_id],
            key=lambda item: (item.start_datetime, item.end_datetime, item.id),
        )
        days_after = _configured_positive_int(settings, 'days_off_after_night_block')
        days_after_penalty = _configured_positive_penalty(
            settings,
            'days_off_after_night_block_penalty_weight',
            DEFAULT_CONSECUTIVE_NIGHTS_PENALTY,
        )
        if days_after is not None:
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
        if days_before_next_night_block is not None:
            for prior_block, next_block in zip(night_blocks, night_blocks[1:]):
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
                ) or DEFAULT_MAX_CONSECUTIVE_NIGHTS,
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
    if violations and total_night_shifts > len(physicians):
        unresolved_reasons.append(
            'Total night assignments exceed active eligible physician count, so some night concentration may be unavoidable.'
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
        if min_shifts is None or min_shifts <= 0:
            continue
        rules.append(
            {
                'period_type': rule.get('period_type') or 'SCHEDULE_BLOCK',
                'minimum': int(min_shifts),
                'penalty_weight': float(
                    _configured_positive_penalty(
                        rule,
                        'min_penalty_weight',
                        DEFAULT_NIGHT_BALANCE_PENALTY,
                    )
                ),
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


def _weekend_volume_rule(contract):
    settings = contract.weekend_settings if isinstance(contract.weekend_settings, dict) else {}
    for rule in settings.get('period_rules') or []:
        if not isinstance(rule, dict):
            continue
        max_volume = _decimal_or_none(rule.get('max_volume'))
        if max_volume is None:
            continue
        penalty = _positive_decimal_or_default(
            rule.get('max_penalty_weight'),
            DEFAULT_WEEKEND_BALANCE_PENALTY,
        )
        return int(max_volume), penalty
    return None, DEFAULT_WEEKEND_BALANCE_PENALTY


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
        max_streak, penalty = _same_shift_rule(contract)
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
    max_streak, penalty = _same_shift_rule(contract)
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


def _distribution_score(
    instances,
    physicians,
    state,
    contract_by_physician,
    eligible_facilities_by_physician,
    include_internal_night_heuristics=False,
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
    weekend_score = Decimal('0')
    facility_distribution_score = Decimal('0')
    total_weekend_assignments = sum(weekend_counts.values())
    default_weekend_target = (
        Decimal(total_weekend_assignments) / Decimal(len(physicians))
        if physicians
        else Decimal('0')
    )

    for physician in physicians:
        physician_instances = assignments_by_physician[physician.id]
        contract = contract_by_physician.get(physician.id)
        if contract is None:
            continue

        assigned_dates = sorted({instance.date for instance in physician_instances})
        consecutive_days_score += _streak_excess_score(
            assigned_dates,
            _workload_rule_limit(
                contract,
                'max_days_in_row',
                DEFAULT_MAX_CONSECUTIVE_DAYS,
            ),
            _workload_rule_penalty(
                contract,
                'max_days_in_row_penalty_weight',
                DEFAULT_CONSECUTIVE_DAYS_PENALTY,
            ),
        )

        facility_counts = defaultdict(int)
        for instance in physician_instances:
            facility_counts[instance.facility_id] += 1

        weekend_limit, weekend_penalty = _weekend_volume_rule(contract)
        weekend_count = Decimal(weekend_counts[physician.id])
        if weekend_limit is not None:
            weekend_score += max(weekend_count - Decimal(weekend_limit), Decimal('0')) * weekend_penalty
        else:
            weekend_excess = max(weekend_count - (default_weekend_target + Decimal('1')), Decimal('0'))
            weekend_score += weekend_excess * weekend_excess * weekend_penalty

        eligible_facility_count = len(eligible_facilities_by_physician.get(physician.id, set()))
        assigned_count = sum(facility_counts.values())
        if eligible_facility_count > 1 and assigned_count > 0 and facility_counts:
            concentration_limit = Decimal(str(ceil(assigned_count * 0.7)))
            concentration_excess = max(
                Decimal(max(facility_counts.values())) - concentration_limit,
                Decimal('0'),
            )
            facility_distribution_score += (
                concentration_excess
                * concentration_excess
                * DEFAULT_FACILITY_CONCENTRATION_PENALTY
            )

    same_shift_score, _violations = _same_shift_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
    )
    night_report = _night_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
        include_internal_heuristics=include_internal_night_heuristics,
    )
    return {
        'consecutive_days_score': consecutive_days_score,
        'same_shift_score': same_shift_score,
        'night_score': night_report['score'],
        'weekend_score': weekend_score,
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
    instance_by_id = {instance.id: instance for instance in instances}
    physician_hours = defaultdict(lambda: Decimal('0'))
    physician_shifts = defaultdict(int)
    physician_night_shifts = defaultdict(int)
    coverage_score = Decimal('0')
    workload_score = Decimal('0')
    request_score_total = Decimal('0')
    request_violations = 0
    request_rewards = 0

    for instance in instances:
        assigned_physician_ids = state[instance.id]
        coverage_score += Decimal(
            max(instance.required_staffing - len(assigned_physician_ids), 0)
        ) * Decimal(COVERAGE_PENALTY)
        for physician_id in assigned_physician_ids:
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
        physicians,
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
    underutilization_score = _underutilization_score(
        physicians,
        physician_shifts,
        eligible_facilities_by_physician,
    )

    for (physician_id, request_date), schedule_requests in requests_by_physician_date.items():
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
                if not any(instance.date == request_date for instance in physician_instances):
                    contract = contract_by_physician.get(physician_id)
                    if contract is not None:
                        request_score_total += _request_weight(contract, schedule_request.weight) / Decimal('2')
                        request_violations += 1
            elif schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_ON:
                template_ids = {template.id for template in schedule_request.shift_templates.all()}
                if not any(
                    instance.date == request_date
                    and instance.shift_template_id in template_ids
                    for instance in physician_instances
                ):
                    contract = contract_by_physician.get(physician_id)
                    if contract is not None:
                        request_score_total += _request_weight(contract, schedule_request.weight) / Decimal('2')
                        request_violations += 1

    validation = _validate_schedule(
        instances,
        physicians,
        state,
        eligible_facilities_by_physician,
        minimum_rest_by_physician,
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
        physicians,
        state,
        contract_by_physician,
        eligible_facilities_by_physician,
        include_internal_night_heuristics=include_internal_night_heuristics,
    )
    _same_shift_score, same_shift_violations = _same_shift_violation_report(
        instances,
        physicians,
        state,
        contract_by_physician,
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

    return {
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
        'request_rewards': request_rewards,
        'physician_hours': physician_hours,
        'physician_shifts': physician_shifts,
        'physician_night_shifts': physician_night_shifts,
        'workload_score_rows': workload_score_rows,
        'validation': validation,
        'same_shift_violations': same_shift_violations,
    }


def _optimizer_pairs(state, manual_pairs):
    pairs = []
    for instance_id, physician_ids in state.items():
        for physician_id in physician_ids:
            if (instance_id, physician_id) not in manual_pairs:
                pairs.append((instance_id, physician_id))
    return sorted(pairs)


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
    return [
        (physician_id, instance_id)
        for _penalty, physician_id, instance_id in sorted(candidates)
    ]


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
        max_streak = _workload_rule_limit(
            contract,
            'max_days_in_row',
            DEFAULT_MAX_CONSECUTIVE_DAYS,
        )
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
        'explanation': _violation_explanation(row_type),
    }


def _report_sort_key(row):
    first_date = row['dates_involved'][0] if row['dates_involved'] else ''
    return (-float(row['penalty_amount'] or 0), row['violation_type'] or '', first_date)


def _validated_night_report_for_current_assignments(night_report, version, assignments):
    assigned_pairs = {
        (assignment.shift_instance_id, assignment.physician_id)
        for assignment in assignments
    }
    instances_by_id = {
        assignment.shift_instance_id: assignment.shift_instance
        for assignment in assignments
    }
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
            instance = instances_by_id.get(instance_id)
            if instance is None:
                validation_errors.append(add_error(violation, f'Shift instance {instance_id} is not currently assigned.'))
                valid = False
                break
            if (instance_id, physician_id) not in assigned_pairs:
                validation_errors.append(add_error(violation, f'Shift instance {instance_id} is not assigned to this physician.'))
                valid = False
                break
            if instance.schedule_version_id != version.id:
                validation_errors.append(add_error(violation, f'Shift instance {instance_id} belongs to a different Schedule Version.'))
                valid = False
                break
            if not (version.schedule_block.start_date <= instance.date <= version.schedule_block.end_date):
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
            instance = instances_by_id.get(instance_id)
            if instance is None or not instance.shift_template.night_shift:
                validation_errors.append(add_error(violation, f'Night block shift instance {instance_id} is not night-designated.'))
                valid = False
                break

        previous_assignment = violation.get('previous_assignment')
        if valid and previous_assignment:
            instance = instances_by_id.get(previous_assignment.get('shift_instance_id'))
            if instance is None or instance.shift_template.night_shift:
                validation_errors.append(add_error(violation, 'Previous assignment boundary is missing or night-designated.'))
                valid = False

        next_assignment = violation.get('next_assignment')
        if valid and next_assignment:
            instance = instances_by_id.get(next_assignment.get('shift_instance_id'))
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


def _assignments_for_optimizer_run(version, optimizer_run=None):
    if optimizer_run is not None and optimizer_run.run_kind == 'COPY':
        query = Q(optimizer_run=optimizer_run)
    else:
        query = Q(
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run__isnull=True,
        )
        if optimizer_run is not None:
            query |= Q(optimizer_run=optimizer_run)
    return ScheduleShiftAssignment.objects.filter(
        query,
        shift_instance__schedule_version=version,
        shift_instance__date__gte=version.schedule_block.start_date,
        shift_instance__date__lte=version.schedule_block.end_date,
    )


def build_violation_report(schedule_version, optimizer_run=None):
    version = (
        ScheduleVersion.objects
        .select_related('schedule_block', 'domain')
        .get(id=schedule_version.id)
    )
    instances = list(
        _version_shift_instances_queryset(version)
        .select_related('facility', 'shift_template')
        .prefetch_related('assignments__physician__user')
        .order_by('date', 'facility__name', 'start_datetime', 'id')
    )
    if optimizer_run is None:
        optimizer_run = version.optimizer_runs.filter(is_active=True).order_by('-run_number').first()
    assignments = list(
        _assignments_for_optimizer_run(version, optimizer_run)
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
        physician.id: _contract_target(
            contract_by_physician[physician.id],
            default_hours_target,
            default_shift_target,
        )
        for physician in physicians
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
        physicians,
        state,
        contract_by_physician,
    )
    night_report = _validated_night_report_for_current_assignments(
        night_report,
        version,
        assignments,
    )
    request_rows = _request_scoring_rows(
        instances,
        physicians,
        state,
        contract_by_physician,
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
        users[physician_id]['workload_score'] = row
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

    for user in users.values():
        user['violations'] = sorted(user['violations'], key=_report_sort_key)

    warnings = []
    if night_report['night_unresolved_reasons']:
        warnings.extend(night_report['night_unresolved_reasons'])
    if night_report['stale_violation_rows_dropped']:
        warnings.append('Dropped stale violation row.')
    warnings.extend(score_audit['warnings'])
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


def recalculate_schedule_version_score(schedule_version, optimizer_run=None):
    """Refresh persisted scoring for the current assignments without optimizing."""
    report = build_violation_report(schedule_version, optimizer_run=optimizer_run)
    workload_summary = []
    for user in report['users']:
        workload = user.get('workload_score') or {}
        workload_summary.append({
            'physician_id': user['user_id'],
            'physician_name': user['display_name'],
            'contract_name': workload.get('contract_name'),
            'assigned_hours': user['hours'],
            'assigned_shifts': user['shifts'],
            'night_shifts': user['night_shifts'],
            'target_units': workload.get('target_units'),
            'target': workload.get('target'),
            'effective_workload_range': workload.get('effective_workload_range'),
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
):
    if schedule_version.status != ScheduleVersion.Status.BUILD:
        raise ValueError('Optimizer can only run on a BUILD Schedule Version.')
    if schedule_version.schedule_block.build_status != ScheduleBlock.BuildStatus.BUILD:
        raise ValueError('Optimizer can only run while the Schedule Block is in BUILD.')

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
                locked_open_shift_instance_ids=source_locked_open_ids,
            )
        else:
            optimizer_run = OptimizerRun.objects.select_for_update().get(
                id=optimizer_run.id,
                schedule_version=version,
            )
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
            .prefetch_related('assignments__physician__user')
            .order_by('date', 'facility__name', 'start_datetime', 'id')
        )
        if source_run is not None:
            source_assignments = list(
                _assignments_for_optimizer_run(version, source_run)
                .select_related('shift_instance', 'physician__user')
            )
        else:
            source_assignments = list(
                _assignments_for_optimizer_run(version, None)
                .select_related('shift_instance', 'physician__user')
            )

        if source_assignments:
            source_assignment_count = len(source_assignments)
            if source_run is None:
                assignments = [
                    row for row in source_assignments
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE or row.is_locked
                ]
            else:
                manual_seed_rows = [
                    ScheduleShiftAssignment(
                        shift_instance_id=row.shift_instance_id,
                        physician_id=row.physician_id,
                        created_by=created_by,
                        assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
                        optimizer_run=optimizer_run,
                        is_locked=row.is_locked,
                    )
                    for row in source_assignments
                    if row.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
                    and (
                        start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                        or row.is_locked
                    )
                ]
                ScheduleShiftAssignment.objects.bulk_create(manual_seed_rows)
                assignments = (
                    [
                        row for row in source_assignments
                        if row.assignment_source == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
                    ]
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    else []
                ) + list(
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

        state, manual_pairs = _state_from_assignments(assignments)
        instances_by_id = {instance.id: instance for instance in instances}

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
            physician.id: _contract_target(
                contract_by_physician[physician.id],
                default_hours_target,
                default_shift_target,
            )
            for physician in physicians
        }
        workload_ranges_by_physician = {}
        workload_totals_by_physician = {}
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
            return runtime_seconds_elapsed() >= MAX_RUNTIME_SECONDS

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
            max_consecutive = _configured_positive_int(
                settings,
                'max_consecutive_night_shifts',
            ) or DEFAULT_MAX_CONSECUTIVE_NIGHTS
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
                        ):
                            rest_violations_blocked += 1
                            continue

                        contract = contract_by_physician[physician.id]
                        target = targets[physician.id]
                        shift_hours = _shift_hours(instance)
                        next_hours = current_scoring['physician_hours'][physician.id] + shift_hours
                        next_shifts = current_scoring['physician_shifts'][physician.id] + 1
                        workload_score = _workload_candidate_score(target, next_hours, next_shifts)
                        workload_rule_delta = _workload_rule_delta_for_candidate(
                            instances,
                            state,
                            instances_by_id,
                            physician.id,
                            instance,
                            target,
                        )
                        same_shift_delta = _same_shift_candidate_delta(
                            instances,
                            physicians,
                            state,
                            contract_by_physician,
                            physician.id,
                            instance,
                        )
                        if NIGHT_BLOCK_BUILDER_ENABLED:
                            night_delta = _night_candidate_delta(
                                instances,
                                physicians,
                                state,
                                contract_by_physician,
                                physician.id,
                                instance,
                            )
                            night_block_bonus = _night_block_extension_bonus(
                                instances_by_id,
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
                            recovery_penalty = _night_recovery_candidate_penalty(
                                instances,
                                physicians,
                                state,
                                contract_by_physician,
                                physician.id,
                                instance,
                            )
                        else:
                            night_delta = Decimal('0')
                            night_block_bonus = Decimal('0')
                            night_minimum_bonus = Decimal('0')
                            recovery_penalty = Decimal('0')
                        if recovery_penalty > 0:
                            recovery_conflict_candidates += 1

                        matching_requests = _requests_for_shift(
                            requests_by_physician_date,
                            physician.id,
                            instance,
                        )
                        request_score, _candidate_violations, _candidate_rewards = _request_score(
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
                        _workload_rank,
                        _workload_rule_delta,
                        _candidate_score,
                        _tie_breaker,
                        selected_physician,
                    ) = min(candidate_pool)
                    _add_to_state(state, instance.id, selected_physician.id)
                    for row in workload_ranges_by_physician[selected_physician.id]:
                        if row['window_start'] <= instance.date <= row['window_end']:
                            key = (row['window_start'], row['window_end'], row['units'])
                            workload_totals_by_physician[selected_physician.id][key] += (
                                Decimal('1') if row['units'] == 'SHIFTS' else _shift_hours(instance)
                            )
                    assignments_made += 1
                    if phase == 'night':
                        night_block_assignment_successes += 1

        if NIGHT_BLOCK_BUILDER_ENABLED:
            build_night_blocks()
        night_instances = shuffle(night_instances)
        non_night_instances = shuffle(non_night_instances)
        if not timed_out:
            fill_open_instances(night_instances, 'night')
        if not timed_out:
            fill_open_instances(non_night_instances, 'non_night')

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
        initial_score = initial_scoring['score']
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
        final_score = initial_score
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
            'request_repair',
            'night_minimum_repair',
            'post_night_recovery_repair',
            'workload_range_repair',
            'same_shift_repair',
            'consecutive_day_repair',
            'general_hill_climb_swaps',
        ]
        phase_attempts = defaultdict(int)
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
        workload_candidate_swaps_considered = 0
        workload_moves_accepted = 0
        workload_swaps_accepted = 0
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
                if trial_scoring['score'] < final_score:
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
                    if trial_scoring['score'] < final_score:
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

            request_candidates = _request_repair_candidates(
                instances,
                physicians,
                state,
                manual_pairs,
                contract_by_physician,
                requests_by_physician_date,
            )
            request_candidates = shuffle(request_candidates)
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
                        bool(_requests_for_shift(requests_by_physician_date, physician.id, instance)),
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
                    if trial_scoring['score'] < final_score:
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
                    if trial_scoring['score'] < final_score:
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
                    if trial_scoring['score'] < final_score:
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
                    if trial_scoring['score'] < final_score:
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
                        if trial_scoring['score'] < final_score:
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
                    if trial_scoring['score'] < final_score:
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
            for from_physician_id, instance_id in same_shift_candidates:
                if improved:
                    break
                if runtime_exceeded():
                    mark_timeout('same_shift_repair')
                    break
                for to_physician_id in low_workload_physicians:
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
                    if trial_scoring['score'] < final_score:
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        same_shift_break_improvements += 1
                        phase_improvements['same_shift_repair'] += 1
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
            consecutive_day_candidates = shuffle(_consecutive_day_break_candidates(
                state,
                instances_by_id,
                manual_pairs,
                contract_by_physician,
            ))
            for from_physician_id, instance_id in consecutive_day_candidates:
                if improved:
                    break
                if runtime_exceeded():
                    mark_timeout('consecutive_day_repair')
                    break
                for to_physician_id in low_workload_physicians:
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
                    if trial_scoring['score'] < final_score:
                        state = trial_state
                        final_score = trial_scoring['score']
                        improvement_count += 1
                        consecutive_day_break_improvements += 1
                        phase_improvements['consecutive_day_repair'] += 1
                        pass_improved = True
                        improved = True
                        break

            improved = False

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
                    if trial_scoring['score'] < final_score:
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
        final_workload_range_counts = _workload_range_counts(final_scoring)
        same_shift_violations_final = len(final_scoring['same_shift_violations'])
        final_night_report = _night_violation_report(
            instances,
            physicians,
            state,
            contract_by_physician,
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

        locked_open_instance_ids = {instance.id for instance in instances if instance.is_locked_open}
        for instance_id in locked_open_instance_ids:
            state[instance_id] = []
        unlocked_manual_ids = [
            assignment.id for assignment in assignments
            if assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and not assignment.is_locked
        ]
        if unlocked_manual_ids:
            ScheduleShiftAssignment.objects.filter(id__in=unlocked_manual_ids).delete()

        for instance in instances:
            optimizer_physician_ids = [
                physician_id
                for physician_id in state[instance.id]
                if (instance.id, physician_id) not in manual_pairs
            ]
            for physician_id in optimizer_physician_ids:
                ScheduleShiftAssignment.objects.create(
                    shift_instance=instance,
                    physician_id=physician_id,
                    created_by=created_by,
                    assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
                    optimizer_run=optimizer_run,
                )

            next_status = (
                ScheduleShiftInstance.Status.ASSIGNED
                if len(state[instance.id]) >= instance.required_staffing
                else ScheduleShiftInstance.Status.OPEN
            )
            if instance.status != next_status:
                instance.status = next_status
                instance.save(update_fields=['status', 'updated_at'])

        unfilled_shift_count = _unfilled_slot_count(instances, state)

    if timed_out:
        message = 'Optimizer stopped after runtime limit. Previous active run preserved.'
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
    final_validation = final_scoring['validation']
    final_request_rows = _request_scoring_rows(
        instances,
        physicians,
        state,
        contract_by_physician,
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
        'initial_score': float(initial_score),
        'final_score': float(final_score),
        'timed_out': timed_out,
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
        'candidate_rest_rejections': rest_violations_blocked,
        'rest_violations_blocked': rest_violations_blocked,
        'debug': {
            'seed': seed,
            'start_mode': start_mode,
            'source_optimizer_run_id': source_run.id if source_run is not None else None,
            'source_assignment_count': source_assignment_count,
            'seeded_assignment_count': len(assignments),
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
            'initial_score': float(initial_score),
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
            'workload_candidate_swaps_considered': workload_candidate_swaps_considered,
            'workload_moves_accepted': workload_moves_accepted,
            'workload_swaps_accepted': workload_swaps_accepted,
            'workload_score_initial': float(initial_scoring['breakdown']['workload_score']),
            'workload_score_final': float(final_scoring['breakdown']['workload_score']),
            'general_swap_attempts': general_swap_attempts,
            'general_swap_improvements': general_swap_improvements,
            'stopped_reason': stopped_reason,
            'runtime_seconds': runtime_seconds,
            'timed_out': timed_out,
            'phase_running_when_stopped': phase_running_when_stopped,
            'candidates_considered_before_timeout': candidates_considered_before_timeout,
            'partial_result_preserved': timed_out,
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
        if not timed_out:
            OptimizerRun.objects.filter(schedule_version=version, is_active=True).exclude(id=optimizer_run.id).update(is_active=False)
        optimizer_run.status = (
            OptimizerRun.Status.FAILED
            if timed_out
            else OptimizerRun.Status.COMPLETED
        )
        optimizer_run.initial_score = summary['initial_score']
        optimizer_run.final_score = summary['final_score']
        optimizer_run.score_breakdown = summary['score_breakdown']
        optimizer_run.optimizer_summary = summary
        optimizer_run.optimizer_debug = summary.get('debug', {})
        optimizer_run.score_is_stale = False
        optimizer_run.is_active = not timed_out
        if timed_out:
            optimizer_run.notes = 'Optimizer stopped after runtime limit. Previous active run preserved.'
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
        if not timed_out:
            version.optimizer_summary = summary
            version.score_is_stale = False
            version.save(update_fields=['optimizer_summary', 'score_is_stale', 'updated_at'])
    return summary
