import json
import random
from copy import copy
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from time import perf_counter

import numpy as np
from django.core.management.base import BaseCommand, CommandError

from apps.scheduling.models import (
    ContractUserAssignment,
    OptimizerRun,
    ScheduleRequest,
)
from apps.scheduling.optimizer import (
    DEFAULT_FACILITY_PROPORTIONALITY_WEIGHT,
    DEFAULT_TIME_PROPORTIONALITY_WEIGHT,
    _copy_state,
    _adaptive_violation_focuses,
    _attach_published_boundary_context,
    _constraint_boundary_padding_days,
    _minimum_rest_hours,
    _night_minimum_reassignment_candidates,
    _night_recovery_conflict_pairs,
    _effective_workload_rule,
    _is_weekend_designated,
    _period_windows,
    _published_boundary_context,
    _request_repair_candidates,
    _request_weight,
    _replace_in_state,
    _score_schedule,
    _same_shift_break_candidates,
    _solve_bounded_multi_physician_neighborhood,
    _selected_physician_score,
    _shift_hours,
    _state_from_assignments,
    _version_contract_target,
    _version_shift_instances_queryset,
    _weekend_repair_candidates,
    _weekend_support_cycle_candidates,
    _consecutive_day_break_candidates,
    evaluate_plateau_pairwise_swap,
    evaluate_plateau_reassignment,
    evaluate_plateau_three_way_rotation,
)
from apps.scheduling.run_state import assignments_for_viewed_run
from apps.scheduling.optimizer_v2 import (
    V2EngineContext,
    legal_reassignment_candidates,
    merge_assignment_rows,
    published_boundary_conflict_matrix,
    schedule_fingerprint,
    select_best_candidate,
    select_diversification_candidate,
    reassign_assignment,
    reassign_assignments,
    rotate_assignments,
    swap_assignments,
)


TIME_BANDS = ('BEFORE_9_AM', '9_AM_TO_9_59_AM', '10_AM_TO_1_59_PM', '2_PM_OR_LATER')


# Process-local handoff between consecutive read-only benchmark neighborhoods.
# The lineage in the key prevents a context from a different search chain from
# contributing cycle history or cached scoring rows to the current chain.
_ENGINE_CONTEXT_CACHE = {}


def _structural_focus_family(violation_type):
    """Map an authoritative violation row to its score-breakdown family."""
    value = str(violation_type or '').upper()
    if value.startswith('REQUEST_'):
        return 'request'
    if 'WEEKEND' in value or value == 'FRIDAY_NIGHT_BEFORE_WEEKEND_OFF':
        return 'weekend'
    if value == 'SAME_SHIFT_STREAK':
        return 'same_shift'
    if value.startswith('SHIFT_GROUP_'):
        return 'shift_rule'
    if 'CONSECUTIVE_DAY' in value or value == 'MAX_DAYS_IN_ROW':
        return 'consecutive'
    if 'NIGHT' in value:
        return 'night'
    return 'workload'


STRUCTURAL_FAMILY_SCORE_COMPONENT = {
    'workload': 'workload_score',
    'request': 'request_score',
    'same_shift': 'same_shift_score',
    'shift_rule': 'shift_rule_score',
    'night': 'night_score',
    'weekend': 'weekend_score',
    'consecutive': 'consecutive_days_score',
}


def _time_band(instance):
    start = getattr(
        instance.shift_template,
        'start_time',
        instance.start_datetime.time(),
    )
    if start.hour < 9:
        return 'BEFORE_9_AM'
    if start.hour < 10:
        return '9_AM_TO_9_59_AM'
    if start.hour < 14:
        return '10_AM_TO_1_59_PM'
    return '2_PM_OR_LATER'


def _governed_template_ids(contract):
    settings = contract.shift_settings if isinstance(contract.shift_settings, dict) else {}
    governed = set()
    for group in settings.get('rules') or ():
        if not isinstance(group, dict):
            continue
        if not any(
            isinstance(rule, dict)
            and (rule.get('min_value') is not None or rule.get('max_value') is not None)
            for rule in (group.get('period_rules') or ())
        ):
            continue
        governed.update(
            int(template_id)
            for template_id in (group.get('shift_template_ids') or ())
            if str(template_id).isdigit()
        )
    return governed


def _dense_shift_rule_profiles(instances, profile_count):
    """Create benchmark-only rule-dense contract profiles without DB writes."""
    template_ids = sorted({instance.shift_template_id for instance in instances})
    profiles = []
    for profile_index in range(profile_count):
        rules = []
        for template_index, template_id in enumerate(template_ids):
            period_type = ('WEEK', 'MONTH', 'SCHEDULE_BLOCK')[
                (profile_index + template_index) % 3
            ]
            rules.append({
                'label': f'Stress template {template_id}',
                'shift_template_ids': [template_id],
                'period_rules': [{
                    'period_type': period_type,
                    'units': 'SHIFTS',
                    'min_value': 0,
                    'max_value': 1 + ((profile_index + template_index) % 4),
                    'min_penalty_weight': 0,
                    'max_penalty_weight': 100 + (profile_index % 5) * 25,
                }],
            })
        profiles.append({'rules': rules})
    return profiles


def _neutral_baseline(weight, opportunities):
    total = float(opportunities.sum())
    if total <= 0:
        return 0.0
    shares = opportunities / total
    return float(weight) * (1.0 - float(np.dot(shares, shares)))


def _workload_penalty(values, minimums, maximums, minimum_weights, maximum_weights):
    return (
        np.maximum(minimums - values, 0.0) * minimum_weights
        + np.maximum(values - maximums, 0.0) * maximum_weights
    )


def _prepare_workload_kernel(
    instances, occupancy, physician_ids, targets,
    shift_for_assignment, physician_for_assignment,
):
    """Compile exact workload rule/window scoring into NumPy lookup tables."""
    shift_dates = np.asarray([instance.date.toordinal() for instance in instances])
    shift_hours = np.asarray([float(_shift_hours(instance)) for instance in instances])
    replacement_by_assignment = np.zeros(
        (len(shift_for_assignment), len(instances)), dtype=np.float64,
    )
    for physician_idx, physician_id in enumerate(physician_ids):
        increments = []
        current_values = []
        minimums = []
        maximums = []
        minimum_weights = []
        maximum_weights = []
        for rule in (targets.get(physician_id) or {}).get('rules') or ():
            for window_start, window_end in _period_windows(
                instances, rule['period_type'],
            ):
                effective = _effective_workload_rule(
                    rule, window_start, window_end,
                )
                in_window = (
                    (shift_dates >= window_start.toordinal())
                    & (shift_dates <= window_end.toordinal())
                )
                increment = in_window.astype(np.float64)
                if effective['units'] != 'SHIFTS':
                    increment *= shift_hours
                increments.append(increment)
                current_values.append(float(increment[occupancy[physician_idx]].sum()))
                minimums.append(
                    float(effective['min_value'])
                    if effective['min_value'] is not None else -np.inf
                )
                maximums.append(
                    float(effective['max_value'])
                    if effective['max_value'] is not None else np.inf
                )
                minimum_weights.append(float(effective['min_penalty_weight']))
                maximum_weights.append(float(effective['max_penalty_weight']))
        if not increments:
            continue
        values = np.asarray(current_values, dtype=np.float64)
        minimums_array = np.asarray(minimums, dtype=np.float64)
        maximums_array = np.asarray(maximums, dtype=np.float64)
        minimum_weights_array = np.asarray(minimum_weights, dtype=np.float64)
        maximum_weights_array = np.asarray(maximum_weights, dtype=np.float64)
        before = _workload_penalty(
            values,
            minimums_array,
            maximums_array,
            minimum_weights_array,
            maximum_weights_array,
        ).sum()
        increment_matrix = np.asarray(increments, dtype=np.float64)
        assignment_indexes = np.flatnonzero(
            physician_for_assignment == physician_idx,
        )
        if assignment_indexes.size:
            old_shifts = shift_for_assignment[assignment_indexes]
            for row_index, old_shift in zip(assignment_indexes, old_shifts):
                after = (
                    values[:, None]
                    - increment_matrix[:, int(old_shift), None]
                    + increment_matrix
                )
                replacement_by_assignment[int(row_index)] = (
                    _workload_penalty(
                        after,
                        minimums_array[:, None],
                        maximums_array[:, None],
                        minimum_weights_array[:, None],
                        maximum_weights_array[:, None],
                    ).sum(axis=0) - before
                )
    return replacement_by_assignment


def _prepare_workload_reassignment_kernel(
    instances, occupancy, physician_ids, targets,
    shift_for_assignment, physician_for_assignment,
):
    """Compile exact workload deltas for removing or adding one shift."""
    shift_dates = np.asarray([instance.date.toordinal() for instance in instances])
    shift_hours = np.asarray([float(_shift_hours(instance)) for instance in instances])
    removal_by_assignment = np.zeros(
        len(shift_for_assignment), dtype=np.float64,
    )
    addition_by_physician_shift = np.zeros(
        (len(physician_ids), len(instances)), dtype=np.float64,
    )
    for physician_idx, physician_id in enumerate(physician_ids):
        increments = []
        current_values = []
        minimums = []
        maximums = []
        minimum_weights = []
        maximum_weights = []
        for rule in (targets.get(physician_id) or {}).get('rules') or ():
            for window_start, window_end in _period_windows(
                instances, rule['period_type'],
            ):
                effective = _effective_workload_rule(
                    rule, window_start, window_end,
                )
                in_window = (
                    (shift_dates >= window_start.toordinal())
                    & (shift_dates <= window_end.toordinal())
                )
                increment = in_window.astype(np.float64)
                if effective['units'] != 'SHIFTS':
                    increment *= shift_hours
                increments.append(increment)
                current_values.append(float(
                    increment[occupancy[physician_idx]].sum()
                ))
                minimums.append(
                    float(effective['min_value'])
                    if effective['min_value'] is not None else -np.inf
                )
                maximums.append(
                    float(effective['max_value'])
                    if effective['max_value'] is not None else np.inf
                )
                minimum_weights.append(float(effective['min_penalty_weight']))
                maximum_weights.append(float(effective['max_penalty_weight']))
        if not increments:
            continue
        values = np.asarray(current_values, dtype=np.float64)
        minimums = np.asarray(minimums, dtype=np.float64)
        maximums = np.asarray(maximums, dtype=np.float64)
        minimum_weights = np.asarray(minimum_weights, dtype=np.float64)
        maximum_weights = np.asarray(maximum_weights, dtype=np.float64)
        increment_matrix = np.asarray(increments, dtype=np.float64)
        before = _workload_penalty(
            values, minimums, maximums, minimum_weights, maximum_weights,
        ).sum()
        addition_by_physician_shift[physician_idx] = (
            _workload_penalty(
                values[:, None] + increment_matrix,
                minimums[:, None],
                maximums[:, None],
                minimum_weights[:, None],
                maximum_weights[:, None],
            ).sum(axis=0) - before
        )
        assignment_indexes = np.flatnonzero(
            physician_for_assignment == physician_idx
        )
        if assignment_indexes.size:
            old_shifts = shift_for_assignment[assignment_indexes]
            after = values[:, None] - increment_matrix[:, old_shifts]
            removal_by_assignment[assignment_indexes] = (
                _workload_penalty(
                    after,
                    minimums[:, None],
                    maximums[:, None],
                    minimum_weights[:, None],
                    maximum_weights[:, None],
                ).sum(axis=0) - before
            )
    return removal_by_assignment, addition_by_physician_shift


def _prepare_shift_rule_kernel(
    instances, occupancy, physician_ids, contracts,
    shift_for_assignment, physician_for_assignment,
):
    """Compile contract shift-group rules into per-physician signature tables."""
    shift_dates = np.asarray([instance.date.toordinal() for instance in instances])
    shift_hours = np.asarray([float(_shift_hours(instance)) for instance in instances])
    shift_templates = np.asarray(
        [instance.shift_template_id for instance in instances], dtype=np.int32,
    )
    replacement_by_assignment = np.zeros(
        (len(shift_for_assignment), len(instances)), dtype=np.float64,
    )
    for physician_idx, physician_id in enumerate(physician_ids):
        contract = contracts[physician_id]
        settings = (
            contract.shift_settings
            if isinstance(contract.shift_settings, dict) else {}
        )
        increments = []
        minimums = []
        maximums = []
        minimum_weights = []
        maximum_weights = []
        for group in settings.get('rules') or ():
            if not isinstance(group, dict):
                continue
            template_ids = {
                int(template_id)
                for template_id in (group.get('shift_template_ids') or ())
                if str(template_id).isdigit()
            }
            if not template_ids:
                continue
            template_mask = np.isin(
                shift_templates,
                np.fromiter(template_ids, dtype=np.int32),
            )
            for rule in group.get('period_rules') or ():
                if not isinstance(rule, dict):
                    continue
                normalized = {
                    'period_type': rule.get('period_type') or 'SCHEDULE_BLOCK',
                    'units': (
                        'SHIFTS' if rule.get('units') == 'SHIFTS' else 'HOURS'
                    ),
                    'min_value': (
                        Decimal(str(rule['min_value']))
                        if rule.get('min_value') not in (None, '') else None
                    ),
                    'max_value': (
                        Decimal(str(rule['max_value']))
                        if rule.get('max_value') not in (None, '') else None
                    ),
                    'min_penalty_weight': Decimal(str(
                        rule.get('min_penalty_weight') or 0,
                    )),
                    'max_penalty_weight': Decimal(str(
                        rule.get('max_penalty_weight') or 0,
                    )),
                }
                if (
                    normalized['min_value'] is None
                    and normalized['max_value'] is None
                ):
                    continue
                for window_start, window_end in _period_windows(
                    instances, normalized['period_type'],
                ):
                    effective = _effective_workload_rule(
                        normalized, window_start, window_end,
                    )
                    increment = (
                        template_mask
                        & (shift_dates >= window_start.toordinal())
                        & (shift_dates <= window_end.toordinal())
                    ).astype(np.float64)
                    if effective['units'] != 'SHIFTS':
                        increment *= shift_hours
                    increments.append(increment)
                    minimums.append(
                        float(effective['min_value'])
                        if effective['min_value'] is not None else -np.inf
                    )
                    maximums.append(
                        float(effective['max_value'])
                        if effective['max_value'] is not None else np.inf
                    )
                    minimum_weights.append(float(effective['min_penalty_weight']))
                    maximum_weights.append(float(effective['max_penalty_weight']))
        if not increments:
            continue
        increment_matrix = np.asarray(increments, dtype=np.float64)
        signature_values, shift_signatures = np.unique(
            increment_matrix.T, axis=0, return_inverse=True,
        )
        current = increment_matrix[:, occupancy[physician_idx]].sum(axis=1)
        minimums = np.asarray(minimums, dtype=np.float64)
        maximums = np.asarray(maximums, dtype=np.float64)
        minimum_weights = np.asarray(minimum_weights, dtype=np.float64)
        maximum_weights = np.asarray(maximum_weights, dtype=np.float64)
        base = _workload_penalty(
            current, minimums, maximums, minimum_weights, maximum_weights,
        ).sum()
        signature_count = len(signature_values)
        replacement = np.zeros(
            (signature_count, signature_count), dtype=np.float64,
        )
        signature_values = signature_values.T
        for old_signature in range(signature_count):
            after = (
                current[:, None]
                - signature_values[:, old_signature, None]
                + signature_values
            )
            replacement[old_signature] = _workload_penalty(
                after,
                minimums[:, None],
                maximums[:, None],
                minimum_weights[:, None],
                maximum_weights[:, None],
            ).sum(axis=0) - base
        assignment_indexes = np.flatnonzero(
            physician_for_assignment == physician_idx,
        )
        if assignment_indexes.size:
            old_signatures = shift_signatures[
                shift_for_assignment[assignment_indexes]
            ]
            replacement_by_assignment[assignment_indexes] = replacement[
                old_signatures[:, None], shift_signatures[None, :]
            ]
    return replacement_by_assignment


def _pair_shift_rule_deltas(
    kernel, shift_for_assignment, physician_for_assignment,
    left_assignment_indexes, right_assignment_indexes,
):
    left = np.asarray(left_assignment_indexes, dtype=np.int32)
    right = np.asarray(right_assignment_indexes, dtype=np.int32)
    return (
        kernel[left, shift_for_assignment[right]]
        + kernel[right, shift_for_assignment[left]]
    )


def _proportionality_score_vector(counts, shares, weight):
    totals = counts.sum(axis=1)
    result = np.zeros(len(counts), dtype=np.float64)
    usable = totals > 0
    if shares.sum() <= 0 or not np.any(usable):
        return result
    deviations = counts[usable] - totals[usable, None] * shares[None, :]
    result[usable] = weight * np.square(deviations).sum(axis=1) / totals[usable]
    return result


def _prepare_proportionality_dimension(
    occupancy, governed, eligible_facility, available_slots,
    shift_categories, shift_facility, category_count, weight,
    shift_for_assignment, physician_for_assignment,
):
    assignment_count = len(shift_for_assignment)
    shift_count = occupancy.shape[1]
    score_delta = np.zeros((assignment_count, shift_count), dtype=np.float64)
    baseline_delta = np.zeros((assignment_count, shift_count), dtype=np.float64)
    base_score_total = 0.0
    base_baseline_total = 0.0
    for physician_idx in range(occupancy.shape[0]):
        unguided = ~governed[physician_idx]
        current_shifts = np.flatnonzero(occupancy[physician_idx] & unguided)
        counts = np.bincount(
            shift_categories[current_shifts], minlength=category_count,
        ).astype(np.float64)
        opportunity_mask = (
            unguided
            & eligible_facility[physician_idx, shift_facility]
            & (available_slots > 0)
        )
        opportunities = np.bincount(
            shift_categories[opportunity_mask],
            weights=available_slots[opportunity_mask],
            minlength=category_count,
        ).astype(np.float64)
        opportunity_total = opportunities.sum()
        shares = (
            opportunities / opportunity_total
            if opportunity_total > 0 else np.zeros(category_count)
        )
        base_score = _proportionality_score_vector(
            counts[None, :], shares, weight,
        )[0]
        neutral = (
            weight * (1.0 - float(np.dot(shares, shares)))
            if opportunity_total > 0 else 0.0
        )
        base_baseline = neutral if counts.sum() > 0 else 0.0
        base_score_total += base_score
        base_baseline_total += base_baseline
        assignment_indexes = np.flatnonzero(
            physician_for_assignment == physician_idx,
        )
        cached = {}
        new_unguided = unguided.astype(np.int8)
        for assignment_idx in assignment_indexes:
            old_shift = int(shift_for_assignment[assignment_idx])
            key = (bool(unguided[old_shift]), int(shift_categories[old_shift]))
            rows = cached.get(key)
            if rows is None:
                after_counts = np.repeat(counts[None, :], shift_count, axis=0)
                if key[0]:
                    after_counts[:, key[1]] -= 1
                after_counts[
                    np.flatnonzero(new_unguided),
                    shift_categories[new_unguided.astype(bool)],
                ] += 1
                after_scores = _proportionality_score_vector(
                    after_counts, shares, weight,
                )
                after_totals = after_counts.sum(axis=1)
                after_baselines = np.where(after_totals > 0, neutral, 0.0)
                rows = (after_scores - base_score, after_baselines - base_baseline)
                cached[key] = rows
            score_delta[assignment_idx] = rows[0]
            baseline_delta[assignment_idx] = rows[1]
    return {
        'score_delta': score_delta,
        'baseline_delta': baseline_delta,
        'base_score': base_score_total,
        'base_baseline': base_baseline_total,
    }


def _proportionality_priority(scores, baselines):
    result = np.zeros_like(np.asarray(scores[0], dtype=np.float64))
    usable_weight = np.zeros_like(result)
    for weight, score, baseline in zip((0.40, 0.60), scores, baselines):
        usable = baseline > 0
        result += np.where(usable, weight * score / np.where(usable, baseline, 1), 0)
        usable_weight += np.where(usable, weight, 0)
    return np.divide(
        result, usable_weight,
        out=np.zeros_like(result), where=usable_weight > 0,
    )


def _workload_replacement_deltas(prepared, physician_idx, old_shifts, new_shifts):
    """Return exact workload score deltas for one physician's replacements."""
    old_shifts = np.asarray(old_shifts, dtype=np.int32)
    new_shifts = np.asarray(new_shifts, dtype=np.int32)
    old_shifts, new_shifts = np.broadcast_arrays(old_shifts, new_shifts)
    result = np.zeros(old_shifts.size, dtype=np.float64)
    rows = prepared[physician_idx]
    if rows is None or not old_shifts.size:
        return result
    after = (
        rows['values'][:, None]
        - rows['increments'][:, old_shifts.ravel()]
        + rows['increments'][:, new_shifts.ravel()]
    )
    result[:] = (
        _workload_penalty(
            after,
            rows['minimums'][:, None],
            rows['maximums'][:, None],
            rows['minimum_weights'][:, None],
            rows['maximum_weights'][:, None],
        ).sum(axis=0)
        - rows['before']
    )
    return result


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _positive_int(value):
    parsed = _number(value)
    return int(parsed) if parsed > 0 else None


def _weekend_score_compact(current, prior, settings, windows, block_bounds, prior_bounds):
    """Mirror authoritative weekend scoring using compact immutable features."""
    current_weekend = [row for row in current if row[1]]
    prior_weekend = [row for row in prior if row[1]]
    score = 0.0
    rules = [
        rule for rule in (settings.get('period_rules') or ())
        if isinstance(rule, dict)
        and (rule.get('min_volume') not in (None, '') or rule.get('max_volume') not in (None, ''))
    ]
    for rule in rules:
        period = rule.get('period_type') or 'SCHEDULE_BLOCK'
        for start, end in windows[period]:
            count = sum(start <= row[0] <= end for row in current_weekend)
            minimum = rule.get('min_volume')
            maximum = rule.get('max_volume')
            if minimum not in (None, ''):
                score += max(_number(minimum) - count, 0.0) * max(
                    _number(rule.get('min_penalty_weight')), 0.0,
                )
            if maximum not in (None, ''):
                score += max(count - _number(maximum), 0.0) * max(
                    _number(rule.get('max_penalty_weight')), 0.0,
                )

    combined_weekend = [(row[0], False) for row in prior_weekend]
    combined_weekend.extend((row[0], True) for row in current_weekend)
    combined_weekend.sort()
    blocks = []
    block = []
    previous = None
    for day, is_current in combined_weekend:
        if previous is None or day in (previous, previous + 1):
            block.append((day, is_current))
        else:
            blocks.append(block)
            block = [(day, is_current)]
        previous = day
    if block:
        blocks.append(block)
    for side in ('min', 'max'):
        limit = _positive_int(settings.get(f'{side}_consecutive_weekend_shifts'))
        weight = max(_number(
            settings.get(f'{side}_consecutive_weekend_shifts_penalty_weight'),
        ), 0.0)
        if limit is None or weight <= 0:
            continue
        for streak in blocks:
            if not any(row[1] for row in streak):
                continue
            actual = len(streak)
            if side == 'min':
                deviation = max(limit - actual, 0)
            else:
                prior_count = sum(not row[1] for row in streak)
                deviation = max(
                    actual - limit - max(prior_count - limit, 0), 0,
                )
            score += deviation * weight

    weekends = {}
    for day, is_current in combined_weekend:
        week = day - ((day - 1) % 7)
        weekends[week] = weekends.get(week, False) or is_current
    weekend_streaks = []
    streak = []
    previous = None
    for week in sorted(weekends):
        if previous is None or week == previous + 7:
            streak.append(week)
        else:
            weekend_streaks.append(streak)
            streak = [week]
        previous = week
    if streak:
        weekend_streaks.append(streak)
    for side in ('min', 'max'):
        limit = _positive_int(settings.get(f'{side}_consecutive_weekends'))
        weight = max(_number(
            settings.get(f'{side}_consecutive_weekends_penalty_weight'),
        ), 0.0)
        if limit is None or weight <= 0:
            continue
        for streak in weekend_streaks:
            if not any(weekends[week] for week in streak):
                continue
            actual = len(streak)
            if side == 'min':
                deviation = max(limit - actual, 0)
            else:
                prior_count = sum(not weekends[week] for week in streak)
                deviation = max(
                    actual - limit - max(prior_count - limit, 0), 0,
                )
            score += deviation * weight

    friday_weight = max(_number(
        settings.get('block_friday_night_before_weekend_off_penalty_weight'),
    ), 0.0)
    if settings.get('block_friday_night_before_weekend_off') and friday_weight > 0:
        all_rows = [*prior, *current]
        friday_nights = {
            row[0] for row in all_rows if row[2] and (row[0] - 1) % 7 == 4
        }
        weekend_days = {row[0] for row in all_rows if row[1]}
        block_start, block_end = block_bounds
        for friday in friday_nights:
            saturday, sunday = friday + 1, friday + 2
            if sunday < block_start or saturday > block_end:
                continue
            if any(
                not (
                    block_start <= day <= block_end
                    or (
                        prior_bounds is not None
                        and prior_bounds[0] <= day <= prior_bounds[1]
                    )
                )
                for day in (saturday, sunday)
            ):
                continue
            if saturday not in weekend_days and sunday not in weekend_days:
                score += friday_weight
    return score


def _prepare_weekend_kernel(
    instances, occupancy, physician_ids, contracts, movable_pairs,
    instance_index, shift_signatures, signature_features,
):
    """Precompute exact fixed-state weekend deltas by replacement signature."""
    period_types = {'SCHEDULE_BLOCK', 'MONTH', 'WEEK'}
    windows = {
        period: [
            (start.toordinal(), end.toordinal())
            for start, end in _period_windows(instances, period)
        ]
        for period in period_types
    }
    block = instances[0].schedule_block
    block_bounds = (block.start_date.toordinal(), block.end_date.toordinal())
    raw_prior_bounds = getattr(instances[0], '_published_boundary_date_range', None)
    prior_bounds = (
        tuple(day.toordinal() for day in raw_prior_bounds)
        if raw_prior_bounds is not None else None
    )
    boundary = _published_boundary_context(instances)
    prior_by_physician = {
        physician_id: [
            (
                instance.date.toordinal(),
                bool(_is_weekend_designated(instance)),
                bool(instance.shift_template.night_shift),
            )
            for instance in boundary.get(physician_id, ())
        ]
        for physician_id in physician_ids
    }
    current_by_physician = []
    settings_by_physician = []
    base_scores = []
    for physician_idx, physician_id in enumerate(physician_ids):
        current = [
            signature_features[int(shift_signatures[shift_idx])]
            for shift_idx in np.flatnonzero(occupancy[physician_idx])
        ]
        settings = contracts[physician_id].weekend_settings
        if not isinstance(settings, dict):
            settings = {}
        current_by_physician.append(current)
        settings_by_physician.append(settings)
        base_scores.append(_weekend_score_compact(
            current,
            prior_by_physician[physician_id],
            settings,
            windows,
            block_bounds,
            prior_bounds,
        ))

    signature_count = len(signature_features)
    deltas = np.zeros((len(movable_pairs), signature_count), dtype=np.float64)
    cache = {}
    physician_lookup = {
        physician_id: physician_idx
        for physician_idx, physician_id in enumerate(physician_ids)
    }
    for assignment_idx, (instance_id, physician_id) in enumerate(movable_pairs):
        physician_idx = physician_lookup[physician_id]
        old_shift = instance_index[instance_id]
        old_signature = int(shift_signatures[old_shift])
        cache_key = (physician_idx, old_signature)
        cached = cache.get(cache_key)
        if cached is None:
            current = list(current_by_physician[physician_idx])
            current.remove(signature_features[old_signature])
            cached = np.zeros(signature_count, dtype=np.float64)
            for new_signature, feature in enumerate(signature_features):
                after = _weekend_score_compact(
                    [*current, feature],
                    prior_by_physician[physician_id],
                    settings_by_physician[physician_idx],
                    windows,
                    block_bounds,
                    prior_bounds,
                )
                cached[new_signature] = after - base_scores[physician_idx]
            cache[cache_key] = cached
        deltas[assignment_idx] = cached
    return deltas


def _unique_period_rules(settings):
    rules = []
    seen = set()
    for rule in settings.get('period_rules') or ():
        if not isinstance(rule, dict):
            continue
        minimum = rule.get('min_shifts')
        maximum = rule.get('max_shifts')
        if minimum in (None, '') and maximum in (None, ''):
            continue
        key = (
            rule.get('period_type') or 'SCHEDULE_BLOCK',
            str(minimum or ''),
            str(maximum or ''),
            str(rule.get('min_penalty_weight') or ''),
            str(rule.get('max_penalty_weight') or ''),
        )
        if key not in seen:
            seen.add(key)
            rules.append(rule)
    return rules


def _night_score_compact(current, prior, settings, windows):
    """Mirror the complete authoritative night score with compact features."""
    current_nights = [row for row in current if row[3]]
    prior_nights = [row for row in prior if row[3]]
    score = 0.0
    period_rules = _unique_period_rules(settings)

    minimum_rows = []
    priority = {'SCHEDULE_BLOCK': 0, 'MONTH': 1, 'WEEK': 2}
    for rule_order, rule in enumerate(period_rules):
        minimum = _number(rule.get('min_shifts'))
        weight = max(_number(rule.get('min_penalty_weight')), 0.0)
        if minimum <= 0 or weight <= 0:
            continue
        period = rule.get('period_type') or 'SCHEDULE_BLOCK'
        for start, end in windows[period]:
            count = sum(start <= row[0] <= end for row in current_nights)
            minimum_rows.append((
                start, end, int(minimum), priority.get(period, 99),
                rule_order, count, weight,
            ))
    seen_minimums = set()
    for start, end, minimum, _priority, _order, count, weight in sorted(
        minimum_rows, key=lambda row: (row[0], row[1], row[2], row[3], row[4]),
    ):
        key = (start, end, minimum)
        if key in seen_minimums:
            continue
        seen_minimums.add(key)
        score += max(minimum - count, 0) * weight

    for rule in period_rules:
        maximum = rule.get('max_shifts')
        if maximum in (None, ''):
            continue
        maximum = int(_number(maximum))
        weight = max(_number(rule.get('max_penalty_weight')), 0.0)
        if weight <= 0:
            continue
        period = rule.get('period_type') or 'SCHEDULE_BLOCK'
        for start, end in windows[period]:
            count = sum(start <= row[0] <= end for row in current_nights)
            score += max(count - maximum, 0) * weight

    nights = sorted(
        [*prior_nights, *current_nights],
        key=lambda row: (row[0], row[1], str(row[4])),
    )
    blocks = []
    block = []
    previous = None
    for row in nights:
        if previous is None or row[0] == previous + 1:
            block.append(row)
        else:
            blocks.append(block)
            block = [row]
        previous = row[0]
    if block:
        blocks.append(block)

    minimum_consecutive = _positive_int(settings.get('min_consecutive_night_shifts'))
    maximum_consecutive = _positive_int(settings.get('max_consecutive_night_shifts'))
    minimum_weight = max(_number(
        settings.get('min_consecutive_night_shifts_penalty_weight'),
    ), 0.0)
    maximum_weight = max(_number(
        settings.get('max_consecutive_night_shifts_penalty_weight'),
    ), 0.0)
    for block in blocks:
        current_count = sum(row[5] for row in block)
        if not current_count:
            continue
        if minimum_consecutive is not None and minimum_weight > 0:
            score += max(minimum_consecutive - len(block), 0) * minimum_weight
        if maximum_consecutive is not None and maximum_weight > 0:
            prior_count = len(block) - current_count
            excess = max(
                len(block) - maximum_consecutive
                - max(prior_count - maximum_consecutive, 0),
                0,
            )
            score += excess * maximum_weight

    assignments = sorted(
        [*prior, *current], key=lambda row: (row[1], row[2], str(row[4])),
    )
    days_after = _positive_int(settings.get('days_off_after_night_block'))
    days_after_weight = max(_number(
        settings.get('days_off_after_night_block_penalty_weight'),
    ), 0.0)
    if days_after is not None and days_after_weight > 0:
        for block in blocks:
            block_keys = {row[4] for row in block}
            block_end = block[-1]
            next_assignment = next((
                row for row in assignments
                if row[4] not in block_keys and not row[3]
                and row[1] >= block_end[2]
            ), None)
            if next_assignment is None or not next_assignment[5]:
                continue
            actual = max(next_assignment[0] - block_end[0] - 1, 0)
            score += max(days_after - actual, 0) * days_after_weight

    days_between = _positive_int(settings.get('days_off_before_next_night_shift'))
    days_between_weight = max(_number(
        settings.get('days_off_before_next_night_shift_penalty_weight'),
    ), 0.0)
    if days_between is not None and days_between_weight > 0:
        for prior_block, next_block in zip(blocks, blocks[1:]):
            if not any(row[5] for row in next_block):
                continue
            actual = max(next_block[0][0] - prior_block[-1][0] - 1, 0)
            score += max(days_between - actual, 0) * days_between_weight
    return score


def _prepare_night_kernel(instances, occupancy, physician_ids, contracts):
    windows = {
        period: [
            (start.toordinal(), end.toordinal())
            for start, end in _period_windows(instances, period)
        ]
        for period in ('SCHEDULE_BLOCK', 'MONTH', 'WEEK')
    }
    shift_features = []
    for shift_idx, instance in enumerate(instances):
        shift_features.append((
            instance.date.toordinal(),
            instance.start_datetime.timestamp(),
            instance.end_datetime.timestamp(),
            bool(instance.shift_template.night_shift),
            ('current', shift_idx),
            True,
        ))
    boundary = _published_boundary_context(instances)
    prepared = []
    for physician_idx, physician_id in enumerate(physician_ids):
        current = [
            shift_features[shift_idx]
            for shift_idx in np.flatnonzero(occupancy[physician_idx])
        ]
        prior = [
            (
                instance.date.toordinal(),
                instance.start_datetime.timestamp(),
                instance.end_datetime.timestamp(),
                bool(instance.shift_template.night_shift),
                ('prior', instance.id),
                False,
            )
            for instance in boundary.get(physician_id, ())
        ]
        settings = contracts[physician_id].night_settings
        if not isinstance(settings, dict):
            settings = {}
        prepared.append({
            'current': current,
            'prior': prior,
            'settings': settings,
            'windows': windows,
            'before': _night_score_compact(current, prior, settings, windows),
            'cache': {},
        })
    return prepared, shift_features


def _night_replacement_delta(prepared, shift_features, physician_idx, old_shift, new_shift):
    rows = prepared[physician_idx]
    key = (int(old_shift), int(new_shift))
    cached = rows['cache'].get(key)
    if cached is not None:
        return cached
    old_key = ('current', int(old_shift))
    current = [row for row in rows['current'] if row[4] != old_key]
    new_row = (*shift_features[int(new_shift)][:4], ('candidate', int(new_shift)), True)
    after = _night_score_compact(
        [*current, new_row], rows['prior'], rows['settings'], rows['windows'],
    )
    delta = after - rows['before']
    rows['cache'][key] = delta
    return delta


def _prepare_night_batch_kernel(instances, occupancy, physician_ids, contracts):
    boundary = _published_boundary_context(instances)
    all_prior = [
        instance
        for physician_id in physician_ids
        for instance in boundary.get(physician_id, ())
    ]
    first_day = min(
        [instance.date.toordinal() for instance in instances]
        + [instance.date.toordinal() for instance in all_prior]
    )
    last_day = max(instance.date.toordinal() for instance in instances)
    day_count = last_day - first_day + 1
    shift_days = np.asarray([
        instance.date.toordinal() - first_day for instance in instances
    ], dtype=np.int16)
    shift_starts = np.asarray([
        instance.start_datetime.timestamp() for instance in instances
    ], dtype=np.float64)
    shift_ends = np.asarray([
        instance.end_datetime.timestamp() for instance in instances
    ], dtype=np.float64)
    shift_nights = np.asarray([
        bool(instance.shift_template.night_shift) for instance in instances
    ], dtype=np.bool_)
    physician_count = len(physician_ids)
    current_nights = np.zeros((physician_count, day_count), dtype=np.int8)
    prior_nights = np.zeros((physician_count, day_count), dtype=np.int8)
    night_end = np.zeros((physician_count, day_count), dtype=np.float64)
    night_second_end = np.zeros((physician_count, day_count), dtype=np.float64)
    assignment_rows = []
    recovery_rows_by_physician = []
    minimum_rows_by_physician = []
    maximum_rows_by_physician = []
    base_scores = np.zeros(physician_count, dtype=np.float64)
    setting_arrays = {
        key: np.zeros(physician_count, dtype=np.float64)
        for key in (
            'min_consecutive_night_shifts',
            'min_consecutive_night_shifts_penalty_weight',
            'max_consecutive_night_shifts',
            'max_consecutive_night_shifts_penalty_weight',
            'days_off_after_night_block',
            'days_off_after_night_block_penalty_weight',
            'days_off_before_next_night_shift',
            'days_off_before_next_night_shift_penalty_weight',
        )
    }
    period_windows = {
        period: [
            (start.toordinal() - first_day, end.toordinal() - first_day)
            for start, end in _period_windows(instances, period)
        ]
        for period in ('SCHEDULE_BLOCK', 'MONTH', 'WEEK')
    }
    priority = {'SCHEDULE_BLOCK': 0, 'MONTH': 1, 'WEEK': 2}
    for physician_idx, physician_id in enumerate(physician_ids):
        current_shift_indexes = np.flatnonzero(occupancy[physician_idx])
        rows = [
            (
                int(shift_days[shift_idx]), shift_starts[shift_idx],
                shift_ends[shift_idx], bool(shift_nights[shift_idx]),
                int(shift_idx), True,
            )
            for shift_idx in current_shift_indexes
        ]
        for shift_idx in current_shift_indexes[shift_nights[current_shift_indexes]]:
            day = int(shift_days[shift_idx])
            current_nights[physician_idx, day] += 1
            ends = sorted((night_end[physician_idx, day], shift_ends[shift_idx]), reverse=True)
            night_end[physician_idx, day], night_second_end[physician_idx, day] = ends
        for instance in boundary.get(physician_id, ()):
            day = instance.date.toordinal() - first_day
            is_night = bool(instance.shift_template.night_shift)
            rows.append((
                day, instance.start_datetime.timestamp(),
                instance.end_datetime.timestamp(), is_night,
                -int(instance.id), False,
            ))
            if is_night:
                prior_nights[physician_idx, day] += 1
                ends = sorted((
                    night_end[physician_idx, day],
                    instance.end_datetime.timestamp(),
                ), reverse=True)
                night_end[physician_idx, day], night_second_end[physician_idx, day] = ends
        assignment_rows.append(rows)
        settings = contracts[physician_id].night_settings
        if not isinstance(settings, dict):
            settings = {}
        compact_current = [
            (
                row[0] + first_day, row[1], row[2], row[3],
                ('current', row[4]), True,
            )
            for row in rows if row[5]
        ]
        compact_prior = [
            (
                row[0] + first_day, row[1], row[2], row[3],
                ('prior', -row[4]), False,
            )
            for row in rows if not row[5]
        ]
        absolute_windows = {
            period: [
                (start + first_day, end + first_day)
                for start, end in windows
            ]
            for period, windows in period_windows.items()
        }
        base_scores[physician_idx] = _night_score_compact(
            compact_current, compact_prior, settings, absolute_windows,
        )
        for key in setting_arrays:
            setting_arrays[key][physician_idx] = max(_number(settings.get(key)), 0.0)

        sorted_assignments = sorted(rows, key=lambda row: (row[1], row[2], row[4]))
        night_rows = sorted(
            (row for row in rows if row[3]),
            key=lambda row: (row[0], row[1], row[4]),
        )
        night_blocks = []
        night_block = []
        previous_day = None
        for row in night_rows:
            if previous_day is None or row[0] == previous_day + 1:
                night_block.append(row)
            else:
                night_blocks.append(night_block)
                night_block = [row]
            previous_day = row[0]
        if night_block:
            night_blocks.append(night_block)
        recovery_rows = []
        for night_block in night_blocks:
            block_end = night_block[-1]
            next_assignments = [
                row for row in sorted_assignments
                if not row[3] and row[1] >= block_end[2]
            ][:2]
            recovery_rows.append((block_end, next_assignments))
        recovery_rows_by_physician.append(recovery_rows)

        minimum_candidates = []
        maximum_rows = []
        for rule_order, rule in enumerate(_unique_period_rules(settings)):
            period = rule.get('period_type') or 'SCHEDULE_BLOCK'
            minimum = _number(rule.get('min_shifts'))
            minimum_weight = max(_number(rule.get('min_penalty_weight')), 0.0)
            maximum = rule.get('max_shifts')
            maximum_weight = max(_number(rule.get('max_penalty_weight')), 0.0)
            for start, end in period_windows[period]:
                if minimum > 0 and minimum_weight > 0:
                    minimum_candidates.append((
                        start, end, int(minimum), minimum_weight,
                        priority.get(period, 99), rule_order,
                    ))
                if maximum not in (None, '') and maximum_weight > 0:
                    maximum_rows.append((
                        start, end, int(_number(maximum)), maximum_weight,
                    ))
        minimum_rows = []
        seen = set()
        for row in sorted(
            minimum_candidates,
            key=lambda item: (item[0], item[1], item[2], item[4], item[5]),
        ):
            key = row[:3]
            if key not in seen:
                seen.add(key)
                minimum_rows.append(row[:4])
        minimum_rows_by_physician.append(minimum_rows)
        maximum_rows_by_physician.append(maximum_rows)

    max_assignments = max(map(len, assignment_rows), default=0)
    assignment_starts = np.full((physician_count, max_assignments), np.inf)
    assignment_days = np.zeros((physician_count, max_assignments), dtype=np.int16)
    assignment_nights = np.ones((physician_count, max_assignments), dtype=np.bool_)
    assignment_current = np.zeros((physician_count, max_assignments), dtype=np.bool_)
    assignment_shift = np.full((physician_count, max_assignments), -1, dtype=np.int32)
    for physician_idx, rows in enumerate(assignment_rows):
        for assignment_idx, row in enumerate(rows):
            assignment_days[physician_idx, assignment_idx] = row[0]
            assignment_starts[physician_idx, assignment_idx] = row[1]
            assignment_nights[physician_idx, assignment_idx] = row[3]
            assignment_shift[physician_idx, assignment_idx] = row[4]
            assignment_current[physician_idx, assignment_idx] = row[5]

    max_recovery_blocks = max(map(len, recovery_rows_by_physician), default=0)
    recovery_valid = np.zeros((physician_count, max_recovery_blocks), dtype=np.bool_)
    recovery_end_days = np.zeros((physician_count, max_recovery_blocks), dtype=np.int16)
    recovery_end_times = np.zeros((physician_count, max_recovery_blocks), dtype=np.float64)
    recovery_next_shift = np.full((physician_count, max_recovery_blocks), -1, dtype=np.int32)
    recovery_next_start = np.full((physician_count, max_recovery_blocks), np.inf)
    recovery_next_day = np.zeros((physician_count, max_recovery_blocks), dtype=np.int16)
    recovery_next_current = np.zeros((physician_count, max_recovery_blocks), dtype=np.bool_)
    recovery_second_shift = np.full((physician_count, max_recovery_blocks), -1, dtype=np.int32)
    recovery_second_start = np.full((physician_count, max_recovery_blocks), np.inf)
    recovery_second_day = np.zeros((physician_count, max_recovery_blocks), dtype=np.int16)
    recovery_second_current = np.zeros((physician_count, max_recovery_blocks), dtype=np.bool_)
    recovery_base_score = np.zeros(physician_count, dtype=np.float64)
    for physician_idx, rows in enumerate(recovery_rows_by_physician):
        days_after = setting_arrays['days_off_after_night_block'][physician_idx]
        weight = setting_arrays['days_off_after_night_block_penalty_weight'][physician_idx]
        for block_idx, (block_end, next_assignments) in enumerate(rows):
            recovery_valid[physician_idx, block_idx] = True
            recovery_end_days[physician_idx, block_idx] = block_end[0]
            recovery_end_times[physician_idx, block_idx] = block_end[2]
            for position, row in enumerate(next_assignments):
                targets = (
                    (
                        recovery_next_shift, recovery_next_start,
                        recovery_next_day, recovery_next_current,
                    )
                    if position == 0 else
                    (
                        recovery_second_shift, recovery_second_start,
                        recovery_second_day, recovery_second_current,
                    )
                )
                targets[0][physician_idx, block_idx] = row[4]
                targets[1][physician_idx, block_idx] = row[1]
                targets[2][physician_idx, block_idx] = row[0]
                targets[3][physician_idx, block_idx] = row[5]
            if next_assignments and next_assignments[0][5]:
                actual = max(next_assignments[0][0] - block_end[0] - 1, 0)
                recovery_base_score[physician_idx] += max(days_after - actual, 0) * weight

    max_minimum_rows = max(map(len, minimum_rows_by_physician), default=0)
    max_maximum_rows = max(map(len, maximum_rows_by_physician), default=0)
    minimum_membership = np.zeros(
        (physician_count, max_minimum_rows, day_count), dtype=np.int8,
    )
    minimum_limits = np.zeros((physician_count, max_minimum_rows))
    minimum_weights = np.zeros((physician_count, max_minimum_rows))
    maximum_membership = np.zeros(
        (physician_count, max_maximum_rows, day_count), dtype=np.int8,
    )
    maximum_limits = np.zeros((physician_count, max_maximum_rows))
    maximum_weights = np.zeros((physician_count, max_maximum_rows))
    for physician_idx, rows in enumerate(minimum_rows_by_physician):
        for row_idx, (start, end, limit, weight) in enumerate(rows):
            minimum_membership[physician_idx, row_idx, start:end + 1] = 1
            minimum_limits[physician_idx, row_idx] = limit
            minimum_weights[physician_idx, row_idx] = weight
    for physician_idx, rows in enumerate(maximum_rows_by_physician):
        for row_idx, (start, end, limit, weight) in enumerate(rows):
            maximum_membership[physician_idx, row_idx, start:end + 1] = 1
            maximum_limits[physician_idx, row_idx] = limit
            maximum_weights[physician_idx, row_idx] = weight
    return {
        'first_day': first_day,
        'shift_days': shift_days,
        'shift_starts': shift_starts,
        'shift_ends': shift_ends,
        'shift_nights': shift_nights,
        'current_nights': current_nights,
        'prior_nights': prior_nights,
        'night_end': night_end,
        'night_second_end': night_second_end,
        'assignment_starts': assignment_starts,
        'assignment_days': assignment_days,
        'assignment_nights': assignment_nights,
        'assignment_current': assignment_current,
        'assignment_shift': assignment_shift,
        'recovery_valid': recovery_valid,
        'recovery_end_days': recovery_end_days,
        'recovery_end_times': recovery_end_times,
        'recovery_next_shift': recovery_next_shift,
        'recovery_next_start': recovery_next_start,
        'recovery_next_day': recovery_next_day,
        'recovery_next_current': recovery_next_current,
        'recovery_second_shift': recovery_second_shift,
        'recovery_second_start': recovery_second_start,
        'recovery_second_day': recovery_second_day,
        'recovery_second_current': recovery_second_current,
        'recovery_base_score': recovery_base_score,
        'minimum_membership': minimum_membership,
        'minimum_limits': minimum_limits,
        'minimum_weights': minimum_weights,
        'maximum_membership': maximum_membership,
        'maximum_limits': maximum_limits,
        'maximum_weights': maximum_weights,
        'base_scores': base_scores,
        **setting_arrays,
    }


def _night_replacement_deltas_batch(kernel, physician_indexes, old_shifts, new_shifts):
    physician_indexes = np.asarray(physician_indexes, dtype=np.int16)
    old_shifts = np.asarray(old_shifts, dtype=np.int32)
    new_shifts = np.asarray(new_shifts, dtype=np.int32)
    size = physician_indexes.size
    if not size:
        return np.zeros(0, dtype=np.float64)
    row_indexes = np.arange(size)
    old_days = kernel['shift_days'][old_shifts]
    new_days = kernel['shift_days'][new_shifts]
    old_nights = kernel['shift_nights'][old_shifts]
    new_nights = kernel['shift_nights'][new_shifts]
    current = kernel['current_nights'][physician_indexes].copy()
    current[row_indexes[old_nights], old_days[old_nights]] -= 1
    current[row_indexes[new_nights], new_days[new_nights]] += 1
    prior = kernel['prior_nights'][physician_indexes]
    total = current + prior
    score = np.zeros(size, dtype=np.float64)

    membership = kernel['minimum_membership'][physician_indexes]
    counts = np.einsum('crd,cd->cr', membership, current, optimize=True)
    score += (
        np.maximum(kernel['minimum_limits'][physician_indexes] - counts, 0.0)
        * kernel['minimum_weights'][physician_indexes]
    ).sum(axis=1)
    membership = kernel['maximum_membership'][physician_indexes]
    counts = np.einsum('crd,cd->cr', membership, current, optimize=True)
    score += (
        np.maximum(counts - kernel['maximum_limits'][physician_indexes], 0.0)
        * kernel['maximum_weights'][physician_indexes]
    ).sum(axis=1)

    candidate_night_end = kernel['night_end'][physician_indexes].copy()
    removing_last = (
        old_nights
        & (candidate_night_end[row_indexes, old_days] == kernel['shift_ends'][old_shifts])
    )
    candidate_night_end[row_indexes[removing_last], old_days[removing_last]] = (
        kernel['night_second_end'][physician_indexes[removing_last], old_days[removing_last]]
    )
    adding_night = new_nights
    candidate_night_end[row_indexes[adding_night], new_days[adding_night]] = np.maximum(
        candidate_night_end[row_indexes[adding_night], new_days[adding_night]],
        kernel['shift_ends'][new_shifts[adding_night]],
    )

    run_length = np.zeros(size, dtype=np.int16)
    run_current = np.zeros(size, dtype=np.int16)
    run_prior = np.zeros(size, dtype=np.int16)
    run_start = np.zeros(size, dtype=np.int16)
    previous_block_end = np.full(size, -10000, dtype=np.int16)
    day_count = total.shape[1]
    min_consecutive = kernel['min_consecutive_night_shifts'][physician_indexes]
    min_weight = kernel['min_consecutive_night_shifts_penalty_weight'][physician_indexes]
    max_consecutive = kernel['max_consecutive_night_shifts'][physician_indexes]
    max_weight = kernel['max_consecutive_night_shifts_penalty_weight'][physician_indexes]
    days_after = kernel['days_off_after_night_block'][physician_indexes]
    days_after_weight = kernel['days_off_after_night_block_penalty_weight'][physician_indexes]
    days_between = kernel['days_off_before_next_night_shift'][physician_indexes]
    days_between_weight = kernel['days_off_before_next_night_shift_penalty_weight'][physician_indexes]
    for day in range(day_count):
        active = total[:, day] > 0
        starting = active & (run_length == 0)
        run_start[starting] = day
        run_length[active] += total[active, day]
        run_current[active] += current[active, day]
        run_prior[active] += prior[active, day]
        next_active = total[:, day + 1] > 0 if day + 1 < day_count else np.zeros(size, dtype=np.bool_)
        ended = active & ~next_active
        if not np.any(ended):
            continue
        applicable = ended & (run_current > 0)
        score[applicable] += (
            np.maximum(min_consecutive[applicable] - run_length[applicable], 0.0)
            * min_weight[applicable]
        )
        score[applicable] += (
            np.maximum(
                run_length[applicable] - max_consecutive[applicable]
                - np.maximum(run_prior[applicable] - max_consecutive[applicable], 0.0),
                0.0,
            ) * max_weight[applicable]
        )
        has_previous = applicable & (previous_block_end >= 0)
        actual_between = run_start - previous_block_end - 1
        score[has_previous] += (
            np.maximum(days_between[has_previous] - actual_between[has_previous], 0.0)
            * days_between_weight[has_previous]
        )

        recovery = ended & (days_after > 0) & (days_after_weight > 0)
        if np.any(recovery):
            recovery_rows = np.flatnonzero(recovery)
            owners = physician_indexes[recovery_rows]
            end_times = candidate_night_end[recovery_rows, day]
            starts = kernel['assignment_starts'][owners]
            valid = (
                ~kernel['assignment_nights'][owners]
                & (starts >= end_times[:, None])
                & (
                    kernel['assignment_shift'][owners]
                    != old_shifts[recovery_rows][:, None]
                )
            )
            choices = np.where(valid, starts, np.inf)
            choice_indexes = choices.argmin(axis=1)
            base_times = choices[np.arange(recovery_rows.size), choice_indexes]
            base_days = kernel['assignment_days'][owners, choice_indexes]
            base_current = kernel['assignment_current'][owners, choice_indexes]
            new_valid = (
                ~new_nights[recovery_rows]
                & (kernel['shift_starts'][new_shifts[recovery_rows]] >= end_times)
            )
            use_new = new_valid & (kernel['shift_starts'][new_shifts[recovery_rows]] < base_times)
            next_days = np.where(use_new, new_days[recovery_rows], base_days)
            next_current = np.where(use_new, True, base_current)
            exists = np.isfinite(np.where(use_new, kernel['shift_starts'][new_shifts[recovery_rows]], base_times))
            applies = exists & next_current
            actual_after = np.maximum(next_days - day - 1, 0)
            selected_rows = recovery_rows[applies]
            score[selected_rows] += (
                np.maximum(days_after[selected_rows] - actual_after[applies], 0.0)
                * days_after_weight[selected_rows]
            )
        previous_block_end[ended] = day
        run_length[ended] = 0
        run_current[ended] = 0
        run_prior[ended] = 0
    return score - kernel['base_scores'][physician_indexes]


def _non_night_recovery_deltas(kernel, physician_indexes, old_shifts, new_shifts):
    """Exact fast path when a replacement does not change any night block."""
    physician_indexes = np.asarray(physician_indexes, dtype=np.int16)
    old_shifts = np.asarray(old_shifts, dtype=np.int32)
    new_shifts = np.asarray(new_shifts, dtype=np.int32)
    valid = kernel['recovery_valid'][physician_indexes]
    next_shift = kernel['recovery_next_shift'][physician_indexes]
    use_second = next_shift == old_shifts[:, None]
    next_start = np.where(
        use_second,
        kernel['recovery_second_start'][physician_indexes],
        kernel['recovery_next_start'][physician_indexes],
    )
    next_day = np.where(
        use_second,
        kernel['recovery_second_day'][physician_indexes],
        kernel['recovery_next_day'][physician_indexes],
    )
    next_current = np.where(
        use_second,
        kernel['recovery_second_current'][physician_indexes],
        kernel['recovery_next_current'][physician_indexes],
    )
    new_start = kernel['shift_starts'][new_shifts][:, None]
    new_day = kernel['shift_days'][new_shifts][:, None]
    use_new = (
        valid
        & (new_start >= kernel['recovery_end_times'][physician_indexes])
        & (new_start < next_start)
    )
    next_start = np.where(use_new, new_start, next_start)
    next_day = np.where(use_new, new_day, next_day)
    next_current = np.where(use_new, True, next_current)
    exists = valid & np.isfinite(next_start) & next_current
    actual = np.maximum(
        next_day - kernel['recovery_end_days'][physician_indexes] - 1,
        0,
    )
    limits = kernel['days_off_after_night_block'][physician_indexes][:, None]
    weights = kernel[
        'days_off_after_night_block_penalty_weight'
    ][physician_indexes][:, None]
    after = (
        np.maximum(limits - actual, 0.0) * weights * exists
    ).sum(axis=1)
    return after - kernel['recovery_base_score'][physician_indexes]


def _night_replacement_deltas_optimized(
    kernel, physician_indexes, old_shifts, new_shifts,
):
    physician_indexes = np.asarray(physician_indexes, dtype=np.int16)
    old_shifts = np.asarray(old_shifts, dtype=np.int32)
    new_shifts = np.asarray(new_shifts, dtype=np.int32)
    result = np.zeros(physician_indexes.size, dtype=np.float64)
    fast = ~kernel['shift_nights'][old_shifts] & ~kernel['shift_nights'][new_shifts]
    if np.any(fast):
        result[fast] = _non_night_recovery_deltas(
            kernel,
            physician_indexes[fast],
            old_shifts[fast],
            new_shifts[fast],
        )
    if np.any(~fast):
        result[~fast] = _night_replacement_deltas_batch(
            kernel,
            physician_indexes[~fast],
            old_shifts[~fast],
            new_shifts[~fast],
        )
    return result


def _pair_night_deltas(
    kernel, shift_for_assignment, physician_for_assignment,
    left_assignment_indexes, right_assignment_indexes, chunk_size=10000,
):
    left_assignment_indexes = np.asarray(left_assignment_indexes, dtype=np.int32)
    right_assignment_indexes = np.asarray(right_assignment_indexes, dtype=np.int32)
    pair_count = left_assignment_indexes.size
    left_shift = shift_for_assignment[left_assignment_indexes]
    right_shift = shift_for_assignment[right_assignment_indexes]
    owners = np.concatenate((
        physician_for_assignment[left_assignment_indexes],
        physician_for_assignment[right_assignment_indexes],
    ))
    old_shifts = np.concatenate((left_shift, right_shift))
    new_shifts = np.concatenate((right_shift, left_shift))
    side_deltas = np.zeros(owners.size, dtype=np.float64)
    fast = (
        ~kernel['shift_nights'][old_shifts]
        & ~kernel['shift_nights'][new_shifts]
    )
    fast_indexes = np.flatnonzero(fast)
    for start in range(0, fast_indexes.size, 100000):
        selected = fast_indexes[start:start + 100000]
        side_deltas[selected] = _non_night_recovery_deltas(
            kernel,
            owners[selected],
            old_shifts[selected],
            new_shifts[selected],
        )
    slow_indexes = np.flatnonzero(~fast)
    for start in range(0, slow_indexes.size, chunk_size):
        selected = slow_indexes[start:start + chunk_size]
        side_deltas[selected] = _night_replacement_deltas_batch(
            kernel,
            owners[selected],
            old_shifts[selected],
            new_shifts[selected],
        )
    return side_deltas[:pair_count] + side_deltas[pair_count:]


def _prepare_request_kernel(
    instances, occupancy, physician_index, contracts, schedule_requests,
):
    physician_count, shift_count = occupancy.shape
    shift_dates = np.asarray([instance.date.toordinal() for instance in instances])
    shift_templates = np.asarray([
        instance.shift_template_id for instance in instances
    ], dtype=np.int32)
    off_cost = np.zeros((physician_count, shift_count), dtype=np.float64)
    on_rows = [[] for _index in range(physician_count)]
    for schedule_request in schedule_requests:
        physician_idx = physician_index.get(schedule_request.physician_id)
        if physician_idx is None:
            continue
        contract = contracts[schedule_request.physician_id]
        weight = float(_request_weight(contract, schedule_request.weight))
        date_mask = shift_dates == schedule_request.date.toordinal()
        request_type = schedule_request.request_type
        if request_type in {
            ScheduleRequest.RequestType.SHIFT_OFF,
            ScheduleRequest.RequestType.SHIFT_ON,
        }:
            template_ids = np.asarray([
                template.id for template in schedule_request.shift_templates.all()
            ], dtype=np.int32)
            mask = date_mask & np.isin(shift_templates, template_ids)
        else:
            mask = date_mask
        if request_type in {
            ScheduleRequest.RequestType.DAY_OFF,
            ScheduleRequest.RequestType.SHIFT_OFF,
        }:
            off_cost[physician_idx, mask] += weight
        elif request_type in {
            ScheduleRequest.RequestType.DAY_ON,
            ScheduleRequest.RequestType.SHIFT_ON,
        }:
            on_rows[physician_idx].append((
                mask,
                int(np.count_nonzero(occupancy[physician_idx] & mask)),
                weight,
            ))
    row_count = max(map(len, on_rows), default=0)
    on_match = np.zeros(
        (physician_count, row_count, shift_count), dtype=np.int8,
    )
    on_counts = np.zeros((physician_count, row_count), dtype=np.int16)
    on_weights = np.zeros((physician_count, row_count), dtype=np.float64)
    for physician_idx, rows in enumerate(on_rows):
        for row_idx, (mask, count, weight) in enumerate(rows):
            on_match[physician_idx, row_idx, mask] = 1
            on_counts[physician_idx, row_idx] = count
            on_weights[physician_idx, row_idx] = weight
    return {
        'off_cost': off_cost,
        'on_match': on_match,
        'on_counts': on_counts,
        'on_weights': on_weights,
    }


def _request_replacement_deltas(kernel, physician_indexes, old_shifts, new_shifts):
    physician_indexes = np.asarray(physician_indexes, dtype=np.int16)
    old_shifts = np.asarray(old_shifts, dtype=np.int32)
    new_shifts = np.asarray(new_shifts, dtype=np.int32)
    result = (
        kernel['off_cost'][physician_indexes, new_shifts]
        - kernel['off_cost'][physician_indexes, old_shifts]
    )
    counts = kernel['on_counts'][physician_indexes]
    after = (
        counts
        - kernel['on_match'][physician_indexes, :, old_shifts]
        + kernel['on_match'][physician_indexes, :, new_shifts]
    )
    weights = kernel['on_weights'][physician_indexes]
    result += (
        ((after == 0).astype(np.int8) - (counts == 0).astype(np.int8))
        * weights
    ).sum(axis=1)
    return result


def _pair_request_deltas(
    kernel, shift_for_assignment, physician_for_assignment,
    left_assignment_indexes, right_assignment_indexes,
):
    left = np.asarray(left_assignment_indexes, dtype=np.int32)
    right = np.asarray(right_assignment_indexes, dtype=np.int32)
    left_shift = shift_for_assignment[left]
    right_shift = shift_for_assignment[right]
    return (
        _request_replacement_deltas(
            kernel, physician_for_assignment[left], left_shift, right_shift,
        )
        + _request_replacement_deltas(
            kernel, physician_for_assignment[right], right_shift, left_shift,
        )
    )


def _request_reassignment_deltas(
    kernel, outgoing_physicians, incoming_physicians, shifts,
):
    """Exact request-score delta for staffing-preserving reassignments."""
    outgoing = np.asarray(outgoing_physicians, dtype=np.int16)
    incoming = np.asarray(incoming_physicians, dtype=np.int16)
    shifts = np.asarray(shifts, dtype=np.int32)
    result = (
        -kernel['off_cost'][outgoing, shifts]
        + kernel['off_cost'][incoming, shifts]
    )
    outgoing_counts = kernel['on_counts'][outgoing]
    outgoing_after = (
        outgoing_counts - kernel['on_match'][outgoing, :, shifts]
    )
    result += (
        (
            (outgoing_after == 0).astype(np.int8)
            - (outgoing_counts == 0).astype(np.int8)
        ) * kernel['on_weights'][outgoing]
    ).sum(axis=1)
    incoming_counts = kernel['on_counts'][incoming]
    incoming_after = (
        incoming_counts + kernel['on_match'][incoming, :, shifts]
    )
    result += (
        (
            (incoming_after == 0).astype(np.int8)
            - (incoming_counts == 0).astype(np.int8)
        ) * kernel['on_weights'][incoming]
    ).sum(axis=1)
    return result


def _streak_penalty_from_days(days, maximum, weight):
    score = 0.0
    run = 0
    previous = None
    for day in sorted(days):
        if previous is not None and day == previous + 1:
            run += 1
        else:
            score += max(run - maximum, 0) * weight
            run = 1
        previous = day
    return score + max(run - maximum, 0) * weight


def _prepare_consecutive_kernel(
    instances, occupancy, physician_ids, contracts, movable_pairs, instance_index,
):
    first_day = instances[0].schedule_block.start_date.toordinal()
    last_day = instances[0].schedule_block.end_date.toordinal()
    day_count = last_day - first_day + 1
    shift_days = np.asarray([
        instance.date.toordinal() - first_day for instance in instances
    ], dtype=np.int16)
    boundary = _published_boundary_context(instances)
    current_counts = np.zeros((len(physician_ids), day_count), dtype=np.int16)
    for physician_idx in range(len(physician_ids)):
        np.add.at(
            current_counts[physician_idx],
            shift_days[np.flatnonzero(occupancy[physician_idx])],
            1,
        )
    physician_lookup = {
        physician_id: physician_idx
        for physician_idx, physician_id in enumerate(physician_ids)
    }
    prior_days = {
        physician_id: {
            instance.date.toordinal() for instance in boundary.get(physician_id, ())
        }
        for physician_id in physician_ids
    }
    rule_by_physician = []
    base_scores = np.zeros(len(physician_ids), dtype=np.float64)
    for physician_idx, physician_id in enumerate(physician_ids):
        settings = contracts[physician_id].workload_settings
        if not isinstance(settings, dict):
            settings = {}
        maximum = _positive_int(settings.get('max_days_in_row'))
        weight = max(_number(settings.get('max_days_in_row_penalty_weight')), 0.0)
        rule_by_physician.append((maximum, weight))
        if maximum is None or weight <= 0:
            continue
        prior = prior_days[physician_id]
        current = {
            first_day + day for day in np.flatnonzero(current_counts[physician_idx])
        }
        base_scores[physician_idx] = max(
            _streak_penalty_from_days(prior | current, maximum, weight)
            - _streak_penalty_from_days(prior, maximum, weight),
            0.0,
        )
    deltas = np.zeros((len(movable_pairs), day_count), dtype=np.float64)
    cache = {}
    for assignment_idx, (instance_id, physician_id) in enumerate(movable_pairs):
        physician_idx = physician_lookup[physician_id]
        maximum, weight = rule_by_physician[physician_idx]
        if maximum is None or weight <= 0:
            continue
        old_day = int(shift_days[instance_index[instance_id]])
        key = (physician_idx, old_day)
        cached = cache.get(key)
        if cached is None:
            counts = current_counts[physician_idx].copy()
            counts[old_day] -= 1
            cached = np.zeros(day_count, dtype=np.float64)
            prior = prior_days[physician_id]
            for new_day in range(day_count):
                counts[new_day] += 1
                current = {
                    first_day + day for day in np.flatnonzero(counts)
                }
                after = max(
                    _streak_penalty_from_days(
                        prior | current, maximum, weight,
                    ) - _streak_penalty_from_days(prior, maximum, weight),
                    0.0,
                )
                cached[new_day] = after - base_scores[physician_idx]
                counts[new_day] -= 1
            cache[key] = cached
        deltas[assignment_idx] = cached
    return deltas, shift_days


def _same_shift_score(indexes, maximum, weight):
    if not indexes:
        return 0.0
    score = 0.0
    run = 1
    previous = None
    for occurrence in sorted(set(indexes)):
        if previous is None:
            previous = occurrence
            continue
        if occurrence == previous + 1:
            run += 1
        else:
            score += max(run - maximum, 0) * weight
            run = 1
        previous = occurrence
    return score + max(run - maximum, 0) * weight


def _prepare_same_shift_kernel(
    instances, occupancy, physician_ids, contracts, movable_pairs, instance_index,
):
    template_instances = defaultdict(list)
    for shift_idx, instance in enumerate(instances):
        template_instances[instance.shift_template_id].append((
            instance.date, instance.start_datetime, instance.id, shift_idx,
        ))
    shift_templates = np.asarray([
        instance.shift_template_id for instance in instances
    ], dtype=np.int32)
    shift_occurrences = np.zeros(len(instances), dtype=np.int16)
    for rows in template_instances.values():
        for occurrence, row in enumerate(sorted(rows)):
            shift_occurrences[row[3]] = occurrence
    indexes = {}
    for physician_idx in range(len(physician_ids)):
        for shift_idx in np.flatnonzero(occupancy[physician_idx]):
            indexes.setdefault(
                (physician_idx, int(shift_templates[shift_idx])), [],
            ).append(int(shift_occurrences[shift_idx]))
    maximums = np.zeros(len(physician_ids), dtype=np.int16)
    weights = np.zeros(len(physician_ids), dtype=np.float64)
    for physician_idx, physician_id in enumerate(physician_ids):
        settings = contracts[physician_id].workload_settings
        if not isinstance(settings, dict):
            settings = {}
        maximum = _positive_int(settings.get('max_same_shifts_in_row'))
        weight = max(_number(
            settings.get('max_same_shifts_in_row_penalty_weight'),
        ), 0.0)
        maximums[physician_idx] = maximum or 0
        weights[physician_idx] = weight
    removal = np.zeros((len(physician_ids), len(instances)), dtype=np.float64)
    addition = np.zeros((len(physician_ids), len(instances)), dtype=np.float64)
    for physician_idx in range(len(physician_ids)):
        maximum = int(maximums[physician_idx])
        weight = weights[physician_idx]
        if not maximum or weight <= 0:
            continue
        for shift_idx in range(len(instances)):
            key = (physician_idx, int(shift_templates[shift_idx]))
            current = indexes.get(key, [])
            before = _same_shift_score(current, maximum, weight)
            occurrence = int(shift_occurrences[shift_idx])
            if occurrence in current:
                after_remove = list(current)
                after_remove.remove(occurrence)
                removal[physician_idx, shift_idx] = (
                    _same_shift_score(after_remove, maximum, weight) - before
                )
            else:
                addition[physician_idx, shift_idx] = (
                    _same_shift_score([*current, occurrence], maximum, weight)
                    - before
                )
    physician_lookup = {
        physician_id: physician_idx
        for physician_idx, physician_id in enumerate(physician_ids)
    }
    replacement_by_assignment = np.zeros(
        (len(movable_pairs), len(instances)), dtype=np.float64,
    )
    for assignment_idx, (instance_id, physician_id) in enumerate(movable_pairs):
        physician_idx = physician_lookup[physician_id]
        old_shift = instance_index[instance_id]
        replacement_by_assignment[assignment_idx] = (
            removal[physician_idx, old_shift] + addition[physician_idx]
        )
        maximum = int(maximums[physician_idx])
        weight = weights[physician_idx]
        if not maximum or weight <= 0:
            continue
        template = int(shift_templates[old_shift])
        current = list(indexes.get((physician_idx, template), []))
        before = _same_shift_score(current, maximum, weight)
        old_occurrence = int(shift_occurrences[old_shift])
        for new_shift in np.flatnonzero(shift_templates == template):
            after_indexes = list(current)
            if old_occurrence in after_indexes:
                after_indexes.remove(old_occurrence)
            after_indexes.append(int(shift_occurrences[new_shift]))
            replacement_by_assignment[assignment_idx, new_shift] = (
                _same_shift_score(after_indexes, maximum, weight) - before
            )
    return {
        'templates': shift_templates,
        'occurrences': shift_occurrences,
        'indexes': indexes,
        'maximums': maximums,
        'weights': weights,
        'removal': removal,
        'addition': addition,
        'replacement_by_assignment': replacement_by_assignment,
        'cache': {},
    }


def _same_shift_replacement_deltas(kernel, physician_indexes, old_shifts, new_shifts):
    physician_indexes = np.asarray(physician_indexes, dtype=np.int16)
    old_shifts = np.asarray(old_shifts, dtype=np.int32)
    new_shifts = np.asarray(new_shifts, dtype=np.int32)
    result = (
        kernel['removal'][physician_indexes, old_shifts]
        + kernel['addition'][physician_indexes, new_shifts]
    )
    same_template = (
        kernel['templates'][old_shifts] == kernel['templates'][new_shifts]
    )
    for position in np.flatnonzero(same_template):
        physician_idx = int(physician_indexes[position])
        old_shift = int(old_shifts[position])
        new_shift = int(new_shifts[position])
        key = (physician_idx, old_shift, new_shift)
        delta = kernel['cache'].get(key)
        if delta is None:
            maximum = int(kernel['maximums'][physician_idx])
            weight = kernel['weights'][physician_idx]
            if not maximum or weight <= 0:
                delta = 0.0
            else:
                template = int(kernel['templates'][old_shift])
                current = list(kernel['indexes'].get((physician_idx, template), []))
                before = _same_shift_score(current, maximum, weight)
                old_occurrence = int(kernel['occurrences'][old_shift])
                new_occurrence = int(kernel['occurrences'][new_shift])
                if old_occurrence in current:
                    current.remove(old_occurrence)
                current.append(new_occurrence)
                delta = _same_shift_score(current, maximum, weight) - before
            kernel['cache'][key] = delta
        result[position] = delta
    return result


def _pair_same_shift_deltas(
    kernel, shift_for_assignment, physician_for_assignment,
    left_assignment_indexes, right_assignment_indexes,
):
    left = np.asarray(left_assignment_indexes, dtype=np.int32)
    right = np.asarray(right_assignment_indexes, dtype=np.int32)
    return (
        kernel['replacement_by_assignment'][
            left, shift_for_assignment[right]
        ]
        + kernel['replacement_by_assignment'][
            right, shift_for_assignment[left]
        ]
    )


class Command(BaseCommand):
    stealth_options = ('runtime_cache',)
    help = (
        'Read-only Atlas v2 stage-one benchmark for vectorized pair-swap '
        'generation, hard feasibility screening, and proportionality deltas.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--run-number', type=int)
        parser.add_argument('--run-id', type=int)
        parser.add_argument('--minimum-rate', type=float, default=600000.0)
        parser.add_argument(
            '--selection-mode',
            choices=('improve', 'diversify'),
            default='improve',
        )
        parser.add_argument('--diversification-seed', type=int, default=0)
        parser.add_argument('--structural-reconstruction', action='store_true')
        parser.add_argument('--reset-engine-context', action='store_true')
        parser.add_argument(
            '--exclude-structural-focus-family', action='append', default=[],
        )
        parser.add_argument('--deep-restart-number', type=int, default=0)
        parser.add_argument(
            '--minimum-diversification-penalty', type=float, default=0.0,
        )
        parser.add_argument(
            '--maximum-diversification-penalty', type=float, default=10000.0,
        )
        parser.add_argument(
            '--exclude-instance-pair', action='append', default=[],
            help='Tabu shift-instance pair as left:right; may be repeated.',
        )
        parser.add_argument('--validate-sample', type=int, default=100)
        parser.add_argument(
            '--checkpoint-score', action='store_true',
            help=(
                'Authoritatively rescore the current state and selected '
                'transition. Continuous production search enables this only '
                'at periodic checkpoints.'
            ),
        )
        parser.add_argument(
            '--validate-refresh', action='store_true',
            help=(
                'Run the expensive accumulated-physician refresh audit. '
                'This is a benchmark diagnostic and is disabled during '
                'production continuous search.'
            ),
        )
        parser.add_argument(
            '--stress-contract-count', type=int, default=0,
            help=(
                'Benchmark-only number of distinct in-memory contract profiles; '
                'each profile governs every shift template. No database writes.'
            ),
        )
        parser.add_argument(
            '--in-memory-swap', action='append', default=[],
            help=(
                'Read-only evolving-state seed as '
                'left_instance:left_physician:right_instance:right_physician; '
                'may be repeated.'
            ),
        )
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args, **options):
        command_started = perf_counter()
        if options['reset_engine_context']:
            _ENGINE_CONTEXT_CACHE.clear()
        run_number = options['run_number']
        run_id = options['run_id']
        if run_id is None and run_number is None:
            raise CommandError('Provide --run-id or --run-number.')
        runtime_cache = options.get('runtime_cache')
        cached_inputs = (
            runtime_cache.get('database_inputs')
            if runtime_cache is not None else None
        )
        if cached_inputs is not None:
            run = cached_inputs['run']
            version = cached_inputs['version']
            instances = cached_inputs['instances']
            assignments = cached_inputs['assignments']
            contract_assignments = cached_inputs['contract_assignments']
        else:
            try:
                runs = OptimizerRun.objects.select_related(
                    'schedule_version__domain',
                )
                run = runs.get(id=run_id) if run_id is not None else runs.get(
                    run_number=run_number,
                )
            except OptimizerRun.DoesNotExist as exc:
                identity = f'id {run_id}' if run_id is not None else run_number
                raise CommandError(f'Optimizer Run {identity} was not found.') from exc

            version = run.schedule_version
            instances = list(
                _version_shift_instances_queryset(version)
                .select_related('facility', 'shift_template')
                .order_by('id')
            )
            assignments = list(
                assignments_for_viewed_run(version, run)
                .select_related('shift_instance', 'physician')
                .order_by('shift_instance_id', 'physician_id', 'id')
            )
            contract_assignments = list(
                ContractUserAssignment.objects
                .filter(
                    domain=version.domain,
                    contract__active=True,
                    physician__active=True,
                )
                .select_related('physician', 'contract')
                .prefetch_related('contract__facilities')
                .order_by('physician_id')
            )
            if runtime_cache is not None:
                runtime_cache['database_inputs'] = {
                    'run': run,
                    'version': version,
                    'instances': instances,
                    'assignments': assignments,
                    'contract_assignments': contract_assignments,
                }
        if not instances or not assignments or not contract_assignments:
            raise CommandError('The selected run does not contain a benchmarkable schedule.')

        cached_base_state = (
            runtime_cache.get('base_state')
            if runtime_cache is not None else None
        )
        if cached_base_state is None:
            base_state, manual_pairs = _state_from_assignments(assignments)
            if runtime_cache is not None:
                runtime_cache['base_state'] = (
                    _copy_state(base_state), frozenset(manual_pairs),
                )
        else:
            base_state, manual_pairs = cached_base_state
        state = _copy_state(base_state)
        applied_in_memory_swaps = []
        for raw_swap in options['in_memory_swap']:
            if str(raw_swap).startswith('C:'):
                values = raw_swap.split(':')
                if len(values) != 10:
                    raise CommandError(
                        'Rotation operations require C plus nine integers.'
                    )
                try:
                    numbers = [int(value) for value in values[1:]]
                except ValueError as exc:
                    raise CommandError(
                        'Rotation operations require C plus nine integers.'
                    ) from exc
                assignment_pairs = [
                    (numbers[0], numbers[1]),
                    (numbers[3], numbers[4]),
                    (numbers[6], numbers[7]),
                ]
                new_physician_ids = [numbers[2], numbers[5], numbers[8]]
                if any(pair in manual_pairs for pair in assignment_pairs):
                    raise CommandError(
                        'In-memory rotations cannot move locked assignments.'
                    )
                try:
                    state = rotate_assignments(
                        state, assignment_pairs, new_physician_ids,
                    )
                except ValueError as exc:
                    raise CommandError(
                        f'In-memory rotation {raw_swap} does not match the '
                        'current state.'
                    ) from exc
                applied_in_memory_swaps.append({
                    'operation': 'rotate',
                    'assignment_pairs': assignment_pairs,
                    'new_physician_ids': new_physician_ids,
                })
                continue
            if str(raw_swap).startswith('R:'):
                try:
                    _marker, instance_id, old_physician_id, new_physician_id = (
                        raw_swap.split(':')
                    )
                    instance_id = int(instance_id)
                    old_physician_id = int(old_physician_id)
                    new_physician_id = int(new_physician_id)
                except (TypeError, ValueError) as exc:
                    raise CommandError(
                        'Reassignment operations require R:instance:old:new.'
                    ) from exc
                if old_physician_id not in state.get(instance_id, ()):
                    raise CommandError(
                        f'In-memory reassignment {raw_swap} does not match '
                        'the current state.'
                    )
                if (instance_id, old_physician_id) in manual_pairs:
                    raise CommandError(
                        'In-memory reassignments cannot move locked assignments.'
                    )
                state = reassign_assignment(
                    state, instance_id, old_physician_id, new_physician_id,
                )
                applied_in_memory_swaps.append({
                    'operation': 'reassign',
                    'instance_id': instance_id,
                    'old_physician_id': old_physician_id,
                    'new_physician_id': new_physician_id,
                })
                continue
            try:
                left_instance_id, left_physician_id, right_instance_id, right_physician_id = (
                    int(value) for value in raw_swap.split(':')
                )
            except (TypeError, ValueError) as exc:
                raise CommandError(
                    '--in-memory-swap requires four colon-separated integers.'
                ) from exc
            if (
                left_physician_id not in state.get(left_instance_id, ())
                or right_physician_id not in state.get(right_instance_id, ())
            ):
                raise CommandError(
                    f'In-memory swap {raw_swap} does not match the current state.'
                )
            if (
                (left_instance_id, left_physician_id) in manual_pairs
                or (right_instance_id, right_physician_id) in manual_pairs
            ):
                raise CommandError('In-memory swaps cannot move locked assignments.')
            state = swap_assignments(
                state,
                (left_instance_id, left_physician_id),
                (right_instance_id, right_physician_id),
            )
            applied_in_memory_swaps.append({
                'operation': 'swap',
                'left_instance_id': left_instance_id,
                'left_physician_id': left_physician_id,
                'right_instance_id': right_instance_id,
                'right_physician_id': right_physician_id,
            })
        stress_contract_count = max(int(options['stress_contract_count']), 0)
        cached_domain_setup = (
            runtime_cache.get('domain_setup')
            if runtime_cache is not None else None
        )
        if cached_domain_setup is None:
            contracts = {
                assignment.physician_id: copy(assignment.contract)
                for assignment in contract_assignments
            }
            physicians = [
                assignment.physician for assignment in contract_assignments
            ]
            manual_only_ids = {
                physician_id
                for physician_id, contract in contracts.items()
                if contract.manual_assignment_only
            }
            physician_ids = sorted(set(contracts) - manual_only_ids)
            if stress_contract_count:
                stress_profiles = _dense_shift_rule_profiles(
                    instances, stress_contract_count,
                )
                for profile_offset, physician_id in enumerate(physician_ids):
                    contracts[physician_id].shift_settings = stress_profiles[
                        profile_offset % stress_contract_count
                    ]
            _attach_published_boundary_context(version, instances, contracts)
            if runtime_cache is not None:
                runtime_cache['domain_setup'] = {
                    'contracts': contracts,
                    'physicians': physicians,
                    'manual_only_ids': manual_only_ids,
                    'physician_ids': physician_ids,
                }
        else:
            contracts = cached_domain_setup['contracts']
            physicians = cached_domain_setup['physicians']
            manual_only_ids = cached_domain_setup['manual_only_ids']
            physician_ids = cached_domain_setup['physician_ids']
        physician_index = {
            physician_id: index for index, physician_id in enumerate(physician_ids)
        }
        instance_index = {
            instance.id: index for index, instance in enumerate(instances)
        }
        facility_ids = sorted({instance.facility_id for instance in instances})
        facility_index = {
            facility_id: index for index, facility_id in enumerate(facility_ids)
        }
        time_index = {band: index for index, band in enumerate(TIME_BANDS)}

        movable_pairs = sorted({
            (instance_id, physician_id)
            for instance_id, physician_ids_in_state in state.items()
            for physician_id in physician_ids_in_state
            if physician_id in physician_index
            and (instance_id, physician_id) not in manual_pairs
        })
        if len(movable_pairs) < 2:
            raise CommandError('The selected run has fewer than two movable assignments.')

        swap_lineage = tuple(
            (
                tuple(
                    ['C']
                    + [
                        value
                        for pair, new_physician_id in zip(
                            row['assignment_pairs'],
                            row['new_physician_ids'],
                        )
                        for value in (
                            pair[0], pair[1], new_physician_id,
                        )
                    ]
                )
                if row.get('operation') == 'rotate'
                else ('R', row['instance_id'], row['old_physician_id'],
                      row['new_physician_id'])
                if row.get('operation') == 'reassign'
                else (
                    'S', row['left_instance_id'], row['left_physician_id'],
                    row['right_instance_id'], row['right_physician_id'],
                )
            )
            for row in applied_in_memory_swaps
        )
        current_fingerprint = schedule_fingerprint(state)
        engine_context_cache_key = (
            run.id, stress_contract_count, swap_lineage, current_fingerprint,
        )
        cached_engine_context = _ENGINE_CONTEXT_CACHE.pop(
            engine_context_cache_key, None,
        )
        if (
            cached_engine_context is not None
            and cached_engine_context.assignment_rows.pairs
            != tuple(movable_pairs)
        ):
            cached_engine_context = None
        engine_context_cache_hit = cached_engine_context is not None

        shift_for_assignment = np.asarray(
            [instance_index[instance_id] for instance_id, _physician_id in movable_pairs],
            dtype=np.int32,
        )
        physician_for_assignment = np.asarray(
            [physician_index[physician_id] for _instance_id, physician_id in movable_pairs],
            dtype=np.int16,
        )
        shift_facility = np.asarray(
            [facility_index[instance.facility_id] for instance in instances],
            dtype=np.int16,
        )
        shift_time = np.asarray(
            [time_index[_time_band(instance)] for instance in instances],
            dtype=np.int8,
        )
        shift_template = np.asarray(
            [instance.shift_template_id for instance in instances],
            dtype=np.int32,
        )
        signature_features = []
        signature_lookup = {}
        shift_signatures = []
        inert_weekend_feature = (
            version.schedule_block.start_date.toordinal(), False, False,
        )
        for instance in instances:
            is_weekend = bool(_is_weekend_designated(instance))
            is_friday_night = (
                instance.date.weekday() == 4
                and bool(instance.shift_template.night_shift)
            )
            feature = (
                (
                    instance.date.toordinal(), is_weekend,
                    bool(instance.shift_template.night_shift),
                )
                if is_weekend or is_friday_night
                else inert_weekend_feature
            )
            if feature not in signature_lookup:
                signature_lookup[feature] = len(signature_features)
                signature_features.append(feature)
            shift_signatures.append(signature_lookup[feature])
        shift_signatures = np.asarray(shift_signatures, dtype=np.int16)
        shift_start = np.asarray(
            [instance.start_datetime.timestamp() for instance in instances],
            dtype=np.float64,
        )
        shift_end = np.asarray(
            [instance.end_datetime.timestamp() for instance in instances],
            dtype=np.float64,
        )

        physician_count = len(physician_ids)
        shift_count = len(instances)
        facility_count = len(facility_ids)
        time_count = len(TIME_BANDS)
        occupancy = np.zeros((physician_count, shift_count), dtype=np.bool_)
        for instance_id, owners in state.items():
            shift_idx = instance_index.get(instance_id)
            if shift_idx is None:
                continue
            for physician_id in owners:
                physician_idx = physician_index.get(physician_id)
                if physician_idx is not None:
                    occupancy[physician_idx, shift_idx] = True

        cached_rules = (
            runtime_cache.get('rules_and_requests')
            if runtime_cache is not None else None
        )
        if cached_rules is None:
            total_required_hours = sum(
                _shift_hours(instance) * instance.required_staffing
                for instance in instances
            )
            total_required_slots = sum(
                instance.required_staffing for instance in instances
            )
            default_hours_target = (
                total_required_hours / Decimal(len(physicians))
                if physicians else Decimal('0')
            )
            default_shift_target = (
                Decimal(total_required_slots) / Decimal(len(physicians))
                if physicians else Decimal('0')
            )
            targets = {
                physician.id: _version_contract_target(
                    version,
                    physician.id,
                    contracts[physician.id],
                    default_hours_target,
                    default_shift_target,
                )
                for physician in physicians
            }
            for physician_id in manual_only_ids:
                targets[physician_id] = {
                    'units': 'HOURS',
                    'target': Decimal('0'),
                    'minimum': Decimal('0'),
                    'maximum': Decimal('0'),
                    'rules': [],
                }
            schedule_requests = list(
                ScheduleRequest.objects
                .filter(
                    schedule_block=version.schedule_block,
                    date__gte=version.schedule_block.start_date,
                    date__lte=version.schedule_block.end_date,
                )
                .prefetch_related('shift_templates')
            )
            requests_by_physician_date = defaultdict(list)
            for schedule_request in schedule_requests:
                requests_by_physician_date[
                    (schedule_request.physician_id, schedule_request.date)
                ].append(schedule_request)
            if runtime_cache is not None:
                runtime_cache['rules_and_requests'] = {
                    'targets': targets,
                    'schedule_requests': schedule_requests,
                    'requests_by_physician_date': requests_by_physician_date,
                }
        else:
            targets = cached_rules['targets']
            schedule_requests = cached_rules['schedule_requests']
            requests_by_physician_date = cached_rules[
                'requests_by_physician_date'
            ]
        preparation_breakdown = {}
        component_started = perf_counter()
        request_kernel = _prepare_request_kernel(
            instances,
            occupancy,
            physician_index,
            contracts,
            schedule_requests,
        )
        preparation_breakdown['request_kernel_seconds'] = (
            perf_counter() - component_started
        )
        first_block_day = version.schedule_block.start_date.toordinal()
        consecutive_shift_days = np.asarray([
            instance.date.toordinal() - first_block_day
            for instance in instances
        ], dtype=np.int16)
        if engine_context_cache_hit:
            cached_tables = cached_engine_context.assignment_rows.tables
            consecutive_kernel = cached_tables['consecutive']
            same_shift_kernel = {
                'replacement_by_assignment': cached_tables['same_shift'],
            }
            workload_kernel = cached_tables['workload']
            shift_rule_kernel = cached_tables['shift_rule']
            weekend_kernel = cached_tables['weekend']
            for name in (
                'consecutive', 'same_shift', 'workload', 'shift_rule', 'weekend',
            ):
                preparation_breakdown[f'{name}_kernel_seconds'] = 0.0
        else:
            component_started = perf_counter()
            consecutive_kernel, consecutive_shift_days = _prepare_consecutive_kernel(
                instances,
                occupancy,
                physician_ids,
                contracts,
                movable_pairs,
                instance_index,
            )
            preparation_breakdown['consecutive_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            same_shift_kernel = _prepare_same_shift_kernel(
                instances,
                occupancy,
                physician_ids,
                contracts,
                movable_pairs,
                instance_index,
            )
            preparation_breakdown['same_shift_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            workload_kernel = _prepare_workload_kernel(
                instances,
                occupancy,
                physician_ids,
                targets,
                shift_for_assignment,
                physician_for_assignment,
            )
            preparation_breakdown['workload_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            shift_rule_kernel = _prepare_shift_rule_kernel(
                instances,
                occupancy,
                physician_ids,
                contracts,
                shift_for_assignment,
                physician_for_assignment,
            )
            preparation_breakdown['shift_rule_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            weekend_kernel = _prepare_weekend_kernel(
                instances,
                occupancy,
                physician_ids,
                contracts,
                movable_pairs,
                instance_index,
                shift_signatures,
                signature_features,
            )
            preparation_breakdown['weekend_kernel_seconds'] = (
                perf_counter() - component_started
            )
        component_started = perf_counter()
        night_batch_kernel = _prepare_night_batch_kernel(
            instances,
            occupancy,
            physician_ids,
            contracts,
        )
        preparation_breakdown['night_kernel_seconds'] = (
            perf_counter() - component_started
        )

        eligible_facility = np.zeros(
            (physician_count, facility_count), dtype=np.bool_,
        )
        governed_template_ids = []
        rest_seconds = np.zeros(physician_count, dtype=np.float64)
        for assignment in contract_assignments:
            physician_idx = physician_index.get(assignment.physician_id)
            if physician_idx is None:
                continue
            for facility in assignment.contract.facilities.all():
                facility_idx = facility_index.get(facility.id)
                if facility_idx is not None:
                    eligible_facility[physician_idx, facility_idx] = True
            while len(governed_template_ids) <= physician_idx:
                governed_template_ids.append(set())
            governed_template_ids[physician_idx] = _governed_template_ids(
                contracts[assignment.physician_id],
            )
            rest_seconds[physician_idx] = float(
                _minimum_rest_hours(assignment.contract),
            ) * 3600.0

        governed = np.zeros((physician_count, shift_count), dtype=np.bool_)
        for physician_idx, template_ids in enumerate(governed_template_ids):
            if template_ids:
                governed[physician_idx] = np.isin(
                    shift_template,
                    np.fromiter(template_ids, dtype=np.int32),
                )

        conflict_counts = np.zeros((physician_count, shift_count), dtype=np.int16)
        for physician_idx in range(physician_count):
            owned = np.flatnonzero(occupancy[physician_idx])
            if not owned.size:
                continue
            rest = rest_seconds[physician_idx]
            conflicts = (
                (shift_end[owned][None, :] + rest > shift_start[:, None])
                & (shift_end[:, None] + rest > shift_start[owned][None, :])
            )
            conflict_counts[physician_idx] = conflicts.sum(axis=1, dtype=np.int16)
        boundary_conflict = published_boundary_conflict_matrix(
            physician_ids,
            shift_start,
            shift_end,
            _published_boundary_context(instances),
            rest_seconds,
        )

        manual_only_occupants = np.zeros(shift_count, dtype=np.int16)
        for instance_id, owners in state.items():
            shift_idx = instance_index.get(instance_id)
            if shift_idx is not None:
                manual_only_occupants[shift_idx] = sum(
                    physician_id in manual_only_ids for physician_id in owners
                )
        available_slots = np.asarray([
            max(int(instance.required_staffing) - int(manual_only_occupants[index]), 0)
            for index, instance in enumerate(instances)
        ], dtype=np.int16)

        facility_counts = np.zeros((physician_count, facility_count), dtype=np.int16)
        time_counts = np.zeros((physician_count, time_count), dtype=np.int16)
        facility_expected = np.zeros((physician_count, facility_count), dtype=np.float64)
        time_expected = np.zeros((physician_count, time_count), dtype=np.float64)
        assigned_totals = np.zeros(physician_count, dtype=np.int16)
        facility_baseline = 0.0
        time_baseline = 0.0
        for physician_idx in range(physician_count):
            unguided_owned = np.flatnonzero(
                occupancy[physician_idx] & ~governed[physician_idx]
            )
            assigned_totals[physician_idx] = unguided_owned.size
            if unguided_owned.size:
                facility_counts[physician_idx] = np.bincount(
                    shift_facility[unguided_owned], minlength=facility_count,
                )
                time_counts[physician_idx] = np.bincount(
                    shift_time[unguided_owned], minlength=time_count,
                )
            opportunity_mask = (
                ~governed[physician_idx]
                & eligible_facility[physician_idx, shift_facility]
                & (available_slots > 0)
            )
            facility_opportunities = np.bincount(
                shift_facility[opportunity_mask],
                weights=available_slots[opportunity_mask],
                minlength=facility_count,
            )
            time_opportunities = np.bincount(
                shift_time[opportunity_mask],
                weights=available_slots[opportunity_mask],
                minlength=time_count,
            )
            if assigned_totals[physician_idx] and facility_opportunities.sum():
                facility_expected[physician_idx] = (
                    assigned_totals[physician_idx]
                    * facility_opportunities / facility_opportunities.sum()
                )
                facility_baseline += _neutral_baseline(
                    DEFAULT_FACILITY_PROPORTIONALITY_WEIGHT,
                    facility_opportunities,
                )
            if assigned_totals[physician_idx] and time_opportunities.sum():
                time_expected[physician_idx] = (
                    assigned_totals[physician_idx]
                    * time_opportunities / time_opportunities.sum()
                )
                time_baseline += _neutral_baseline(
                    DEFAULT_TIME_PROPORTIONALITY_WEIGHT,
                    time_opportunities,
                )

        facility_deviation = facility_counts.astype(np.float64) - facility_expected
        time_deviation = time_counts.astype(np.float64) - time_expected
        component_started = perf_counter()
        if engine_context_cache_hit:
            cached_tables = cached_engine_context.assignment_rows.tables
            facility_scores = np.divide(
                np.square(facility_deviation).sum(axis=1),
                assigned_totals,
                out=np.zeros(physician_count, dtype=np.float64),
                where=assigned_totals > 0,
            ) * float(DEFAULT_FACILITY_PROPORTIONALITY_WEIGHT)
            time_scores = np.divide(
                np.square(time_deviation).sum(axis=1),
                assigned_totals,
                out=np.zeros(physician_count, dtype=np.float64),
                where=assigned_totals > 0,
            ) * float(DEFAULT_TIME_PROPORTIONALITY_WEIGHT)
            facility_proportionality_kernel = {
                'score_delta': cached_tables['facility_proportionality_score'],
                'baseline_delta': cached_tables[
                    'facility_proportionality_baseline'
                ],
                'base_score': float(facility_scores.sum()),
                'base_baseline': float(facility_baseline),
            }
            time_proportionality_kernel = {
                'score_delta': cached_tables['time_proportionality_score'],
                'baseline_delta': cached_tables['time_proportionality_baseline'],
                'base_score': float(time_scores.sum()),
                'base_baseline': float(time_baseline),
            }
        else:
            facility_proportionality_kernel = _prepare_proportionality_dimension(
                occupancy, governed, eligible_facility, available_slots,
                shift_facility, shift_facility, facility_count,
                float(DEFAULT_FACILITY_PROPORTIONALITY_WEIGHT),
                shift_for_assignment, physician_for_assignment,
            )
            time_proportionality_kernel = _prepare_proportionality_dimension(
                occupancy, governed, eligible_facility, available_slots,
                shift_time, shift_facility, time_count,
                float(DEFAULT_TIME_PROPORTIONALITY_WEIGHT),
                shift_for_assignment, physician_for_assignment,
            )
        preparation_breakdown['proportionality_kernel_seconds'] = (
            perf_counter() - component_started
        )
        base_proportionality_priority = float(_proportionality_priority(
            (
                facility_proportionality_kernel['base_score'],
                time_proportionality_kernel['base_score'],
            ),
            (
                facility_proportionality_kernel['base_baseline'],
                time_proportionality_kernel['base_baseline'],
            ),
        ))
        if engine_context_cache_hit:
            engine_context = cached_engine_context
            if engine_context.fingerprint != current_fingerprint:
                raise CommandError(
                    'The cached v2 engine context does not match the current state.'
                )
        else:
            engine_context = V2EngineContext.create(
                state,
                movable_pairs,
                {
                    'workload': workload_kernel,
                    'shift_rule': shift_rule_kernel,
                    'consecutive': consecutive_kernel,
                    'weekend': weekend_kernel,
                    'same_shift': same_shift_kernel['replacement_by_assignment'],
                    'facility_proportionality_score': (
                        facility_proportionality_kernel['score_delta']
                    ),
                    'facility_proportionality_baseline': (
                        facility_proportionality_kernel['baseline_delta']
                    ),
                    'time_proportionality_score': (
                        time_proportionality_kernel['score_delta']
                    ),
                    'time_proportionality_baseline': (
                        time_proportionality_kernel['baseline_delta']
                    ),
                },
            )
        workload_kernel = engine_context.assignment_rows.tables['workload']
        shift_rule_kernel = engine_context.assignment_rows.tables['shift_rule']
        consecutive_kernel = engine_context.assignment_rows.tables['consecutive']
        weekend_kernel = engine_context.assignment_rows.tables['weekend']
        same_shift_kernel['replacement_by_assignment'] = (
            engine_context.assignment_rows.tables['same_shift']
        )
        facility_proportionality_kernel['score_delta'] = (
            engine_context.assignment_rows.tables[
                'facility_proportionality_score'
            ]
        )
        facility_proportionality_kernel['baseline_delta'] = (
            engine_context.assignment_rows.tables[
                'facility_proportionality_baseline'
            ]
        )
        time_proportionality_kernel['score_delta'] = (
            engine_context.assignment_rows.tables['time_proportionality_score']
        )
        time_proportionality_kernel['baseline_delta'] = (
            engine_context.assignment_rows.tables[
                'time_proportionality_baseline'
            ]
        )

        def prepare_affected_assignment_tables(next_state, affected_ids, new_pairs):
            refresh_breakdown = {}
            affected_ids = sorted(set(affected_ids))
            affected_set = set(affected_ids)
            affected_global_indexes = np.asarray([
                physician_index[physician_id] for physician_id in affected_ids
            ], dtype=np.int16)
            affected_physician_index = {
                physician_id: index
                for index, physician_id in enumerate(affected_ids)
            }
            affected_occupancy = np.zeros(
                (len(affected_ids), shift_count), dtype=np.bool_,
            )
            for instance_id, owners in next_state.items():
                shift_idx = instance_index.get(instance_id)
                if shift_idx is None:
                    continue
                for physician_id in owners:
                    affected_idx = affected_physician_index.get(physician_id)
                    if affected_idx is not None:
                        affected_occupancy[affected_idx, shift_idx] = True
            refreshed_pairs = [
                pair for pair in new_pairs if pair[1] in affected_set
            ]
            refreshed_shift_for_assignment = np.asarray([
                instance_index[instance_id]
                for instance_id, _physician_id in refreshed_pairs
            ], dtype=np.int32)
            refreshed_physician_for_assignment = np.asarray([
                affected_physician_index[physician_id]
                for _instance_id, physician_id in refreshed_pairs
            ], dtype=np.int16)
            refresh_component_started = perf_counter()
            refreshed_consecutive, _shift_days = _prepare_consecutive_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                refreshed_pairs, instance_index,
            )
            refresh_breakdown['consecutive_seconds'] = (
                perf_counter() - refresh_component_started
            )
            refresh_component_started = perf_counter()
            refreshed_same_shift = _prepare_same_shift_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                refreshed_pairs, instance_index,
            )
            refresh_breakdown['same_shift_seconds'] = (
                perf_counter() - refresh_component_started
            )
            refresh_component_started = perf_counter()
            refreshed_workload = _prepare_workload_kernel(
                instances, affected_occupancy, affected_ids, targets,
                refreshed_shift_for_assignment,
                refreshed_physician_for_assignment,
            )
            refresh_breakdown['workload_seconds'] = (
                perf_counter() - refresh_component_started
            )
            refresh_component_started = perf_counter()
            refreshed_shift_rule = _prepare_shift_rule_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                refreshed_shift_for_assignment,
                refreshed_physician_for_assignment,
            )
            refresh_breakdown['shift_rule_seconds'] = (
                perf_counter() - refresh_component_started
            )
            refresh_component_started = perf_counter()
            refreshed_weekend = _prepare_weekend_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                refreshed_pairs, instance_index, shift_signatures,
                signature_features,
            )
            refresh_breakdown['weekend_seconds'] = (
                perf_counter() - refresh_component_started
            )
            refresh_component_started = perf_counter()
            refreshed_facility = _prepare_proportionality_dimension(
                affected_occupancy,
                governed[affected_global_indexes],
                eligible_facility[affected_global_indexes],
                available_slots,
                shift_facility,
                shift_facility,
                facility_count,
                float(DEFAULT_FACILITY_PROPORTIONALITY_WEIGHT),
                refreshed_shift_for_assignment,
                refreshed_physician_for_assignment,
            )
            refresh_breakdown['facility_proportionality_seconds'] = (
                perf_counter() - refresh_component_started
            )
            refresh_component_started = perf_counter()
            refreshed_time = _prepare_proportionality_dimension(
                affected_occupancy,
                governed[affected_global_indexes],
                eligible_facility[affected_global_indexes],
                available_slots,
                shift_time,
                shift_facility,
                time_count,
                float(DEFAULT_TIME_PROPORTIONALITY_WEIGHT),
                refreshed_shift_for_assignment,
                refreshed_physician_for_assignment,
            )
            refresh_breakdown['time_proportionality_seconds'] = (
                perf_counter() - refresh_component_started
            )
            return refreshed_pairs, {
                'workload': refreshed_workload,
                'shift_rule': refreshed_shift_rule,
                'consecutive': refreshed_consecutive,
                'weekend': refreshed_weekend,
                'same_shift': refreshed_same_shift['replacement_by_assignment'],
                'facility_proportionality_score': refreshed_facility['score_delta'],
                'facility_proportionality_baseline': (
                    refreshed_facility['baseline_delta']
                ),
                'time_proportionality_score': refreshed_time['score_delta'],
                'time_proportionality_baseline': refreshed_time['baseline_delta'],
            }, refresh_breakdown

        started = perf_counter()
        preparation_seconds = started - command_started
        distinct_states = 0
        hard_feasible_states = 0
        scored_states = 0
        improving_states = 0
        workload_neutral_states = 0
        workload_nonworsening_improving_states = 0
        weekend_neutral_states = 0
        workload_weekend_nonworsening_improving_states = 0
        night_neutral_states = 0
        request_neutral_states = 0
        consecutive_neutral_states = 0
        same_shift_neutral_states = 0
        completed_components_nonworsening_improving_states = 0
        best_vectorized_candidate = None
        best_selection = None
        reassignment_shortlist = []
        cycle_shortlist = []
        reassignment_candidate_schedules = 0
        night_left_assignment_batches = []
        night_right_assignment_batches = []
        night_priority_batches = []
        night_partial_penalty_batches = []
        validation_pool = []
        validation_sample_limit = max(int(options['validate_sample']), 0)
        assignment_count = len(movable_pairs)
        for left_index in range(assignment_count - 1):
            right_index = np.arange(left_index + 1, assignment_count, dtype=np.int32)
            left_shift = int(shift_for_assignment[left_index])
            left_physician = int(physician_for_assignment[left_index])
            right_shift = shift_for_assignment[right_index]
            right_physician = physician_for_assignment[right_index]

            distinct = (
                (right_shift != left_shift)
                & (right_physician != left_physician)
                & ~occupancy[left_physician, right_shift]
                & ~occupancy[right_physician, left_shift]
            )
            distinct_states += int(np.count_nonzero(distinct))
            if not np.any(distinct):
                continue

            legal = (
                distinct
                & eligible_facility[left_physician, shift_facility[right_shift]]
                & eligible_facility[right_physician, shift_facility[left_shift]]
            )
            right_conflicts_left_owner = (
                (shift_end[left_shift] + rest_seconds[left_physician] > shift_start[right_shift])
                & (shift_end[right_shift] + rest_seconds[left_physician] > shift_start[left_shift])
            )
            left_conflicts_right_owner = (
                (shift_end[right_shift] + rest_seconds[right_physician] > shift_start[left_shift])
                & (shift_end[left_shift] + rest_seconds[right_physician] > shift_start[right_shift])
            )
            legal &= (
                conflict_counts[left_physician, right_shift]
                - right_conflicts_left_owner.astype(np.int16)
                == 0
            )
            legal &= (
                conflict_counts[right_physician, left_shift]
                - left_conflicts_right_owner.astype(np.int16)
                == 0
            )
            legal &= ~boundary_conflict[left_physician, right_shift]
            legal &= ~boundary_conflict[right_physician, left_shift]
            hard_feasible_states += int(np.count_nonzero(legal))

            scoreable = legal
            selected = np.flatnonzero(scoreable)
            if not selected.size:
                continue
            scored_states += int(selected.size)
            selected_right_physician = right_physician[selected]
            selected_right_shift = right_shift[selected]

            workload_delta = (
                workload_kernel[left_index, selected_right_shift]
                + workload_kernel[right_index[selected], left_shift]
            )
            workload_neutral = np.abs(workload_delta) <= 1e-9
            workload_neutral_states += int(np.count_nonzero(workload_neutral))
            weekend_delta = (
                weekend_kernel[left_index, shift_signatures[selected_right_shift]]
                + weekend_kernel[
                    right_index[selected], shift_signatures[left_shift]
                ]
            )
            weekend_neutral = np.abs(weekend_delta) <= 1e-9
            weekend_neutral_states += int(np.count_nonzero(weekend_neutral))
            selected_right_indexes = right_index[selected]
            facility_delta = (
                facility_proportionality_kernel['score_delta'][
                    left_index, selected_right_shift
                ]
                + facility_proportionality_kernel['score_delta'][
                    selected_right_indexes, left_shift
                ]
            )
            time_delta = (
                time_proportionality_kernel['score_delta'][
                    left_index, selected_right_shift
                ]
                + time_proportionality_kernel['score_delta'][
                    selected_right_indexes, left_shift
                ]
            )
            facility_baseline_delta = (
                facility_proportionality_kernel['baseline_delta'][
                    left_index, selected_right_shift
                ]
                + facility_proportionality_kernel['baseline_delta'][
                    selected_right_indexes, left_shift
                ]
            )
            time_baseline_delta = (
                time_proportionality_kernel['baseline_delta'][
                    left_index, selected_right_shift
                ]
                + time_proportionality_kernel['baseline_delta'][
                    selected_right_indexes, left_shift
                ]
            )
            priority_delta = _proportionality_priority(
                (
                    facility_proportionality_kernel['base_score'] + facility_delta,
                    time_proportionality_kernel['base_score'] + time_delta,
                ),
                (
                    facility_proportionality_kernel['base_baseline']
                    + facility_baseline_delta,
                    time_proportionality_kernel['base_baseline']
                    + time_baseline_delta,
                ),
            ) - base_proportionality_priority
            improving_states += int(np.count_nonzero(priority_delta < -1e-12))
            workload_nonworsening_improving_states += int(np.count_nonzero(
                (priority_delta < -1e-12) & (workload_delta <= 1e-9)
            ))
            workload_weekend_nonworsening_improving_states += int(
                np.count_nonzero(
                    (priority_delta < -1e-12)
                    & ((workload_delta + weekend_delta) <= 1e-9)
                )
            )
            night_left_assignment_batches.append(
                np.full(selected.size, left_index, dtype=np.int32)
            )
            night_right_assignment_batches.append(right_index[selected])
            night_priority_batches.append(priority_delta)
            night_partial_penalty_batches.append(workload_delta + weekend_delta)
            # Retain one legal candidate per left assignment across the entire
            # neighborhood, not only proportionality improvements. The final
            # seeded draw therefore validates improving, worsening, and neutral
            # official-score directions without early-date sampling bias.
            sample_index = int(
                (left_index * 2654435761) % selected.size
            )
            sample_offset = int(selected[sample_index])
            sample_right_index = int(right_index[sample_offset])
            validation_pool.append({
                'left_instance_id': movable_pairs[left_index][0],
                'left_physician_id': movable_pairs[left_index][1],
                'right_instance_id': movable_pairs[sample_right_index][0],
                'right_physician_id': movable_pairs[sample_right_index][1],
                'predicted_priority_delta': float(priority_delta[sample_index]),
                'predicted_workload_delta': float(workload_delta[sample_index]),
                'predicted_weekend_delta': float(weekend_delta[sample_index]),
                'left_assignment_index': left_index,
                'right_assignment_index': sample_right_index,
            })

        if night_left_assignment_batches:
            night_left_assignments = np.concatenate(night_left_assignment_batches)
            night_right_assignments = np.concatenate(night_right_assignment_batches)
            night_deltas = _pair_night_deltas(
                night_batch_kernel,
                shift_for_assignment,
                physician_for_assignment,
                night_left_assignments,
                night_right_assignments,
            )
            request_deltas = _pair_request_deltas(
                request_kernel,
                shift_for_assignment,
                physician_for_assignment,
                night_left_assignments,
                night_right_assignments,
            )
            consecutive_deltas = (
                consecutive_kernel[
                    night_left_assignments,
                    consecutive_shift_days[
                        shift_for_assignment[night_right_assignments]
                    ],
                ]
                + consecutive_kernel[
                    night_right_assignments,
                    consecutive_shift_days[
                        shift_for_assignment[night_left_assignments]
                    ],
                ]
            )
            same_shift_deltas = _pair_same_shift_deltas(
                same_shift_kernel,
                shift_for_assignment,
                physician_for_assignment,
                night_left_assignments,
                night_right_assignments,
            )
            shift_rule_deltas = _pair_shift_rule_deltas(
                shift_rule_kernel,
                shift_for_assignment,
                physician_for_assignment,
                night_left_assignments,
                night_right_assignments,
            )
            night_priorities = np.concatenate(night_priority_batches)
            night_partial_penalties = np.concatenate(night_partial_penalty_batches)
            night_neutral_states = int(np.count_nonzero(
                np.abs(night_deltas) <= 1e-9
            ))
            request_neutral_states = int(np.count_nonzero(
                np.abs(request_deltas) <= 1e-9
            ))
            consecutive_neutral_states = int(np.count_nonzero(
                np.abs(consecutive_deltas) <= 1e-9
            ))
            same_shift_neutral_states = int(np.count_nonzero(
                np.abs(same_shift_deltas) <= 1e-9
            ))
            completed_components_nonworsening_improving_states = int(
                np.count_nonzero(
                    (night_priorities < -1e-12)
                    & ((
                        night_partial_penalties + night_deltas + request_deltas
                        + consecutive_deltas + same_shift_deltas
                        + shift_rule_deltas
                    ) <= 1e-9)
                )
            )
            official_deltas = (
                night_partial_penalties + night_deltas + request_deltas
                + consecutive_deltas + same_shift_deltas + shift_rule_deltas
            )
            excluded_instance_pairs = set()
            for raw_pair in options['exclude_instance_pair']:
                try:
                    first, second = (int(value) for value in raw_pair.split(':'))
                except (TypeError, ValueError) as exc:
                    raise CommandError(
                        '--exclude-instance-pair requires two '
                        'colon-separated integers.'
                    ) from exc
                excluded_instance_pairs.add(tuple(sorted((first, second))))
            if excluded_instance_pairs:
                excluded = np.fromiter((
                    tuple(sorted((
                        movable_pairs[int(left_index)][0],
                        movable_pairs[int(right_index)][0],
                    ))) in excluded_instance_pairs
                    for left_index, right_index in zip(
                        night_left_assignments, night_right_assignments,
                    )
                ), dtype=np.bool_, count=len(official_deltas))
                official_deltas = official_deltas.copy()
                night_priorities = night_priorities.copy()
                official_deltas[excluded] = np.inf
                night_priorities[excluded] = np.inf
            selection_mode = options['selection_mode']
            if selection_mode == 'diversify':
                best_selection = select_diversification_candidate(
                    official_deltas,
                    night_priorities,
                    night_left_assignments,
                    night_right_assignments,
                    seed=options['diversification_seed'],
                    minimum_penalty_increase=options[
                        'minimum_diversification_penalty'
                    ],
                    maximum_penalty_increase=options[
                        'maximum_diversification_penalty'
                    ],
                )
            else:
                best_selection = select_best_candidate(
                    official_deltas,
                    night_priorities,
                    night_left_assignments,
                    night_right_assignments,
                )
            if best_selection is not None:
                best_index = best_selection.candidate_index
                best_left = best_selection.left_assignment_index
                best_right = best_selection.right_assignment_index
                best_vectorized_candidate = {
                    'left_assignment_index': best_left,
                    'right_assignment_index': best_right,
                    'left_instance_id': movable_pairs[best_left][0],
                    'left_physician_id': movable_pairs[best_left][1],
                    'right_instance_id': movable_pairs[best_right][0],
                    'right_physician_id': movable_pairs[best_right][1],
                    'predicted_official_delta': best_selection.official_delta,
                    'predicted_proportionality_delta': (
                        best_selection.proportionality_delta
                    ),
                    'selection_mode': selection_mode,
                }

        # Pair swaps preserve each physician's workload. When that neighborhood
        # is exhausted, expose the V1 request-repair tactic as a first-class V2
        # move: enumerate every hard-feasible one-way reassignment in NumPy,
        # rank request relief, then authoritatively score only a small shortlist.
        if best_selection is None and options['selection_mode'] == 'improve':
            reassignment_assignment_indexes, reassignment_new_physicians = (
                legal_reassignment_candidates(
                    shift_for_assignment,
                    physician_for_assignment,
                    occupancy,
                    eligible_facility,
                    shift_facility,
                    conflict_counts,
                    boundary_conflict,
                )
            )
            if reassignment_assignment_indexes.size:
                reassignment_candidate_schedules = int(
                    reassignment_assignment_indexes.size
                )
                reassignment_shifts = shift_for_assignment[
                    reassignment_assignment_indexes
                ]
                reassignment_old_physicians = physician_for_assignment[
                    reassignment_assignment_indexes
                ]
                reassignment_request_deltas = _request_reassignment_deltas(
                    request_kernel,
                    reassignment_old_physicians,
                    reassignment_new_physicians,
                    reassignment_shifts,
                )
                component_started = perf_counter()
                (
                    workload_removal_deltas,
                    workload_addition_deltas,
                ) = _prepare_workload_reassignment_kernel(
                    instances,
                    occupancy,
                    physician_ids,
                    targets,
                    shift_for_assignment,
                    physician_for_assignment,
                )
                preparation_breakdown[
                    'workload_reassignment_kernel_seconds'
                ] = perf_counter() - component_started
                reassignment_workload_deltas = (
                    workload_removal_deltas[
                        reassignment_assignment_indexes
                    ]
                    + workload_addition_deltas[
                        reassignment_new_physicians,
                        reassignment_shifts,
                    ]
                )
                request_improvements = np.flatnonzero(
                    reassignment_request_deltas < -1e-9
                )
                workload_improvements = np.flatnonzero(
                    reassignment_workload_deltas < -1e-9
                )
                instances_by_id_for_sources = {
                    instance.id: instance for instance in instances
                }
                source_kinds_by_pair = {}
                source_groups = (
                    ('request', _request_repair_candidates(
                        instances,
                        physicians,
                        state,
                        manual_pairs,
                        contracts,
                        requests_by_physician_date,
                    )),
                    ('weekend', _weekend_repair_candidates(
                        instances,
                        physicians,
                        state,
                        manual_pairs,
                        contracts,
                    )),
                    ('same_shift', _same_shift_break_candidates(
                        instances,
                        physicians,
                        state,
                        manual_pairs,
                        contracts,
                    )),
                    ('consecutive_days', _consecutive_day_break_candidates(
                        state,
                        instances_by_id_for_sources,
                        manual_pairs,
                        contracts,
                    )),
                    ('night_recovery', _night_recovery_conflict_pairs(
                        instances,
                        physicians,
                        state,
                        manual_pairs,
                        contracts,
                    )),
                )
                for source_kind, source_pairs in source_groups:
                    for source_physician_id, source_instance_id in source_pairs:
                        source_kinds_by_pair.setdefault(
                            (source_instance_id, source_physician_id),
                            source_kind,
                        )
                directed_source_candidates = np.asarray([
                    candidate_index
                    for candidate_index, assignment_index in enumerate(
                        reassignment_assignment_indexes
                    )
                    if movable_pairs[int(assignment_index)]
                    in source_kinds_by_pair
                ], dtype=np.int32)
                ordered_groups = []
                if request_improvements.size:
                    ordered_groups.append(request_improvements[np.argsort(
                        reassignment_request_deltas[request_improvements],
                        kind='stable',
                    )[:128]])
                if workload_improvements.size:
                    ordered_groups.append(workload_improvements[np.argsort(
                        reassignment_workload_deltas[workload_improvements],
                        kind='stable',
                    )[:128]])
                if directed_source_candidates.size:
                    directed_priority = (
                        reassignment_request_deltas[directed_source_candidates]
                        + reassignment_workload_deltas[
                            directed_source_candidates
                        ]
                    )
                    ordered_groups.append(directed_source_candidates[np.argsort(
                        directed_priority,
                        kind='stable',
                    )[:128]])
                seen_reassignments = set()
                for ordered in ordered_groups:
                    for candidate_index in ordered:
                        assignment_index = int(
                            reassignment_assignment_indexes[candidate_index]
                        )
                        incoming_index = int(
                            reassignment_new_physicians[candidate_index]
                        )
                        instance_id, old_physician_id = movable_pairs[
                            assignment_index
                        ]
                        identity = (
                            instance_id,
                            old_physician_id,
                            physician_ids[incoming_index],
                        )
                        if identity in seen_reassignments:
                            continue
                        seen_reassignments.add(identity)
                        reassignment_shortlist.append({
                            'operation': 'reassign',
                            'instance_id': instance_id,
                            'old_physician_id': old_physician_id,
                            'new_physician_id': physician_ids[incoming_index],
                            'source': source_kinds_by_pair.get(
                                (instance_id, old_physician_id),
                                'workload',
                            ),
                            'predicted_request_delta': float(
                                reassignment_request_deltas[candidate_index]
                            ),
                            'predicted_workload_delta': float(
                                reassignment_workload_deltas[candidate_index]
                            ),
                        })
                legal_reassignment_indexes = {}
                for candidate_index, (
                    assignment_index, incoming_index,
                ) in enumerate(zip(
                    reassignment_assignment_indexes,
                    reassignment_new_physicians,
                )):
                    instance_id, old_physician_id = movable_pairs[
                        int(assignment_index)
                    ]
                    legal_reassignment_indexes[
                        (
                            instance_id,
                            old_physician_id,
                            physician_ids[int(incoming_index)],
                        )
                    ] = candidate_index
                for candidate in _night_minimum_reassignment_candidates(
                    instances,
                    physicians,
                    state,
                    manual_pairs,
                    contracts,
                    candidate_limit=128,
                ):
                    identity = (
                        candidate['instance_id'],
                        candidate['old_physician_id'],
                        candidate['new_physician_id'],
                    )
                    candidate_index = legal_reassignment_indexes.get(identity)
                    if candidate_index is None or identity in seen_reassignments:
                        continue
                    seen_reassignments.add(identity)
                    reassignment_shortlist.append({
                        **candidate,
                        'operation': 'reassign',
                        'predicted_request_delta': float(
                            reassignment_request_deltas[candidate_index]
                        ),
                        'predicted_workload_delta': float(
                            reassignment_workload_deltas[candidate_index]
                        ),
                    })
            cycle_shortlist = _weekend_support_cycle_candidates(
                instances,
                physicians,
                state,
                manual_pairs,
                contracts,
                candidate_limit=128,
            )

        elapsed = perf_counter() - started
        engine_context = engine_context.record_neighborhood(distinct_states)
        rate = distinct_states / elapsed if elapsed else 0.0
        target = float(options['minimum_rate'])
        affected_refresh = None
        if applied_in_memory_swaps and options['validate_refresh']:
            affected_ids = sorted({
                physician_id
                for swap in applied_in_memory_swaps
                for physician_id in (
                    (
                        tuple(
                            pair[1] for pair in swap['assignment_pairs']
                        )
                    ) if swap.get('operation') == 'rotate' else (
                        swap['old_physician_id'], swap['new_physician_id']
                    ) if swap.get('operation') == 'reassign' else (
                        swap['left_physician_id'], swap['right_physician_id']
                    )
                )
                if physician_id in physician_index
            })
            affected_set = set(affected_ids)
            affected_global_indexes = np.asarray(
                [physician_index[physician_id] for physician_id in affected_ids],
                dtype=np.int16,
            )
            affected_occupancy = occupancy[affected_global_indexes]
            affected_pairs = [
                pair for pair in movable_pairs if pair[1] in affected_set
            ]
            affected_assignment_indexes = np.asarray([
                assignment_idx
                for assignment_idx, pair in enumerate(movable_pairs)
                if pair[1] in affected_set
            ], dtype=np.int32)
            affected_physician_index = {
                physician_id: index
                for index, physician_id in enumerate(affected_ids)
            }
            affected_shift_for_assignment = np.asarray([
                instance_index[instance_id]
                for instance_id, _physician_id in affected_pairs
            ], dtype=np.int32)
            affected_physician_for_assignment = np.asarray([
                affected_physician_index[physician_id]
                for _instance_id, physician_id in affected_pairs
            ], dtype=np.int16)
            affected_refresh_breakdown = {}
            refresh_started = perf_counter()

            component_started = perf_counter()
            affected_request_kernel = _prepare_request_kernel(
                instances, affected_occupancy, affected_physician_index,
                contracts, schedule_requests,
            )
            affected_refresh_breakdown['request_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            affected_consecutive_kernel, affected_consecutive_shift_days = (
                _prepare_consecutive_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                affected_pairs, instance_index,
                )
            )
            affected_refresh_breakdown['consecutive_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            affected_same_shift_kernel = _prepare_same_shift_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                affected_pairs, instance_index,
            )
            affected_refresh_breakdown['same_shift_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            affected_workload_kernel = _prepare_workload_kernel(
                instances, affected_occupancy, affected_ids, targets,
                affected_shift_for_assignment,
                affected_physician_for_assignment,
            )
            affected_refresh_breakdown['workload_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            affected_shift_rule_kernel = _prepare_shift_rule_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                affected_shift_for_assignment,
                affected_physician_for_assignment,
            )
            affected_refresh_breakdown['shift_rule_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            affected_weekend_kernel = _prepare_weekend_kernel(
                instances, affected_occupancy, affected_ids, contracts,
                affected_pairs, instance_index, shift_signatures,
                signature_features,
            )
            affected_refresh_breakdown['weekend_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            affected_night_kernel = _prepare_night_batch_kernel(
                instances, affected_occupancy, affected_ids, contracts,
            )
            affected_refresh_breakdown['night_kernel_seconds'] = (
                perf_counter() - component_started
            )
            component_started = perf_counter()
            affected_facility_proportionality = _prepare_proportionality_dimension(
                affected_occupancy,
                governed[affected_global_indexes],
                eligible_facility[affected_global_indexes],
                available_slots,
                shift_facility,
                shift_facility,
                facility_count,
                float(DEFAULT_FACILITY_PROPORTIONALITY_WEIGHT),
                affected_shift_for_assignment,
                affected_physician_for_assignment,
            )
            affected_time_proportionality = _prepare_proportionality_dimension(
                affected_occupancy,
                governed[affected_global_indexes],
                eligible_facility[affected_global_indexes],
                available_slots,
                shift_time,
                shift_facility,
                time_count,
                float(DEFAULT_TIME_PROPORTIONALITY_WEIGHT),
                affected_shift_for_assignment,
                affected_physician_for_assignment,
            )
            affected_refresh_breakdown['proportionality_kernel_seconds'] = (
                perf_counter() - component_started
            )

            def max_abs_error(left, right):
                difference = np.abs(np.asarray(left) - np.asarray(right))
                return float(difference.max()) if difference.size else 0.0

            refresh_errors = {
                'workload': max_abs_error(
                    affected_workload_kernel,
                    workload_kernel[affected_assignment_indexes],
                ),
                'shift_rule': max_abs_error(
                    affected_shift_rule_kernel,
                    shift_rule_kernel[affected_assignment_indexes],
                ),
                'consecutive': max_abs_error(
                    affected_consecutive_kernel,
                    consecutive_kernel[affected_assignment_indexes],
                ),
                'weekend': max_abs_error(
                    affected_weekend_kernel,
                    weekend_kernel[affected_assignment_indexes],
                ),
                'same_shift': max_abs_error(
                    affected_same_shift_kernel['replacement_by_assignment'],
                    same_shift_kernel['replacement_by_assignment'][
                        affected_assignment_indexes
                    ],
                ),
                'request_off': max_abs_error(
                    affected_request_kernel['off_cost'],
                    request_kernel['off_cost'][affected_global_indexes],
                ),
                'facility_proportionality_score': max_abs_error(
                    affected_facility_proportionality['score_delta'],
                    facility_proportionality_kernel['score_delta'][
                        affected_assignment_indexes
                    ],
                ),
                'facility_proportionality_baseline': max_abs_error(
                    affected_facility_proportionality['baseline_delta'],
                    facility_proportionality_kernel['baseline_delta'][
                        affected_assignment_indexes
                    ],
                ),
                'time_proportionality_score': max_abs_error(
                    affected_time_proportionality['score_delta'],
                    time_proportionality_kernel['score_delta'][
                        affected_assignment_indexes
                    ],
                ),
                'time_proportionality_baseline': max_abs_error(
                    affected_time_proportionality['baseline_delta'],
                    time_proportionality_kernel['baseline_delta'][
                        affected_assignment_indexes
                    ],
                ),
            }
            for name, current_rows, refreshed_rows in (
                ('merged_workload', workload_kernel, affected_workload_kernel),
                ('merged_shift_rule', shift_rule_kernel, affected_shift_rule_kernel),
                ('merged_consecutive', consecutive_kernel, affected_consecutive_kernel),
                ('merged_weekend', weekend_kernel, affected_weekend_kernel),
                (
                    'merged_same_shift',
                    same_shift_kernel['replacement_by_assignment'],
                    affected_same_shift_kernel['replacement_by_assignment'],
                ),
                (
                    'merged_facility_proportionality_score',
                    facility_proportionality_kernel['score_delta'],
                    affected_facility_proportionality['score_delta'],
                ),
                (
                    'merged_facility_proportionality_baseline',
                    facility_proportionality_kernel['baseline_delta'],
                    affected_facility_proportionality['baseline_delta'],
                ),
                (
                    'merged_time_proportionality_score',
                    time_proportionality_kernel['score_delta'],
                    affected_time_proportionality['score_delta'],
                ),
                (
                    'merged_time_proportionality_baseline',
                    time_proportionality_kernel['baseline_delta'],
                    affected_time_proportionality['baseline_delta'],
                ),
            ):
                refresh_errors[name] = max_abs_error(
                    merge_assignment_rows(
                        current_rows,
                        movable_pairs,
                        movable_pairs,
                        refreshed_rows,
                        affected_pairs,
                    ),
                    current_rows,
                )
            local_left = np.arange(
                max(len(affected_pairs) - 1, 0), dtype=np.int32,
            )
            local_right = local_left + 1
            different_owners = (
                affected_physician_for_assignment[local_left]
                != affected_physician_for_assignment[local_right]
            )
            local_left = local_left[different_owners][:100]
            local_right = local_right[different_owners][:100]
            if local_left.size:
                global_left = affected_assignment_indexes[local_left]
                global_right = affected_assignment_indexes[local_right]
                refresh_errors['request_pairs'] = max_abs_error(
                    _pair_request_deltas(
                        affected_request_kernel,
                        affected_shift_for_assignment,
                        affected_physician_for_assignment,
                        local_left,
                        local_right,
                    ),
                    _pair_request_deltas(
                        request_kernel,
                        shift_for_assignment,
                        physician_for_assignment,
                        global_left,
                        global_right,
                    ),
                )
                refresh_errors['night_pairs'] = max_abs_error(
                    _pair_night_deltas(
                        affected_night_kernel,
                        affected_shift_for_assignment,
                        affected_physician_for_assignment,
                        local_left,
                        local_right,
                    ),
                    _pair_night_deltas(
                        night_batch_kernel,
                        shift_for_assignment,
                        physician_for_assignment,
                        global_left,
                        global_right,
                    ),
                )
            affected_refresh = {
                'physician_count': len(affected_ids),
                'movable_assignments': len(affected_pairs),
                'seconds': perf_counter() - refresh_started,
                'breakdown': affected_refresh_breakdown,
                'max_abs_errors': refresh_errors,
                'exact_match': all(error <= 1e-9 for error in refresh_errors.values()),
            }
        if validation_pool and validation_sample_limit:
            rng = np.random.default_rng(20260929)
            chosen = rng.choice(
                len(validation_pool),
                size=min(validation_sample_limit, len(validation_pool)),
                replace=False,
            )
            validation_sample = [validation_pool[int(index)] for index in chosen]
            sampled_night_deltas = _pair_night_deltas(
                night_batch_kernel,
                shift_for_assignment,
                physician_for_assignment,
                [row['left_assignment_index'] for row in validation_sample],
                [row['right_assignment_index'] for row in validation_sample],
            )
            sampled_request_deltas = _pair_request_deltas(
                request_kernel,
                shift_for_assignment,
                physician_for_assignment,
                [row['left_assignment_index'] for row in validation_sample],
                [row['right_assignment_index'] for row in validation_sample],
            )
            sampled_left = np.asarray([
                row['left_assignment_index'] for row in validation_sample
            ], dtype=np.int32)
            sampled_right = np.asarray([
                row['right_assignment_index'] for row in validation_sample
            ], dtype=np.int32)
            sampled_consecutive_deltas = (
                consecutive_kernel[
                    sampled_left,
                    consecutive_shift_days[shift_for_assignment[sampled_right]],
                ]
                + consecutive_kernel[
                    sampled_right,
                    consecutive_shift_days[shift_for_assignment[sampled_left]],
                ]
            )
            sampled_same_shift_deltas = _pair_same_shift_deltas(
                same_shift_kernel,
                shift_for_assignment,
                physician_for_assignment,
                sampled_left,
                sampled_right,
            )
            sampled_shift_rule_deltas = _pair_shift_rule_deltas(
                shift_rule_kernel,
                shift_for_assignment,
                physician_for_assignment,
                sampled_left,
                sampled_right,
            )
            for row, night_delta, request_delta, consecutive_delta, same_delta, shift_rule_delta in zip(
                validation_sample,
                sampled_night_deltas,
                sampled_request_deltas,
                sampled_consecutive_deltas,
                sampled_same_shift_deltas,
                sampled_shift_rule_deltas,
            ):
                row['predicted_night_delta'] = float(night_delta)
                row['predicted_request_delta'] = float(request_delta)
                row['predicted_consecutive_delta'] = float(consecutive_delta)
                row['predicted_same_shift_delta'] = float(same_delta)
                row['predicted_shift_rule_delta'] = float(shift_rule_delta)
        else:
            validation_sample = []

        validation_started = perf_counter()
        legal_mismatches = 0
        score_neutral_candidates = 0
        proportionality_direction_matches = 0
        proportionality_direction_mismatches = 0
        component_delta_mismatches = 0
        workload_delta_mismatches = 0
        weekend_delta_mismatches = 0
        night_delta_mismatches = 0
        request_delta_mismatches = 0
        consecutive_delta_mismatches = 0
        same_shift_delta_mismatches = 0
        shift_rule_delta_mismatches = 0
        official_delta_mismatches = 0
        excluded_component_nonzero_candidates = 0
        component_delta_stats = defaultdict(lambda: {
            'nonzero_candidates': 0,
            'positive_candidates': 0,
            'negative_candidates': 0,
            'absolute_delta_total': 0.0,
            'maximum_positive_delta': 0.0,
            'minimum_negative_delta': 0.0,
        })
        exact_results = []
        transition_validation = None
        structural_reconstruction_attempt = None
        in_process_transition = None
        authoritative_current_score = None
        if (
            best_vectorized_candidate is not None
            or reassignment_shortlist
            or cycle_shortlist
            or validation_sample
            or options['checkpoint_score']
        ):
            eligible_facilities_by_physician = {
                assignment.physician_id: {
                    facility.id for facility in assignment.contract.facilities.all()
                }
                for assignment in contract_assignments
            }
            minimum_rest_by_physician = {
                assignment.physician_id: _minimum_rest_hours(assignment.contract)
                for assignment in contract_assignments
            }
            score_checkpoint = bool(
                options['checkpoint_score']
                or options['structural_reconstruction']
                or validation_sample
                or reassignment_shortlist
                or cycle_shortlist
            )
            current_scoring = None
            if score_checkpoint:
                current_scoring = _score_schedule(
                    instances,
                    physicians,
                    state,
                    targets,
                    contracts,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                )
                authoritative_current_score = float(current_scoring['score'])
            instances_by_id = {instance.id: instance for instance in instances}
            locked_open_instance_ids = {
                instance.id for instance in instances if instance.is_locked_open
            }
            if (
                options['structural_reconstruction']
                and options['selection_mode'] == 'diversify'
            ):
                reconstruction_rng = random.Random(
                    int(options['diversification_seed'])
                )
                focuses = _adaptive_violation_focuses(
                    instances=instances,
                    physicians=physicians,
                    state=state,
                    scoring=current_scoring,
                    contract_by_physician=contracts,
                    requests_by_physician_date=requests_by_physician_date,
                )
                focuses_by_family = defaultdict(list)
                for focus_row in focuses:
                    focuses_by_family[_structural_focus_family(
                        focus_row.get('violation_type')
                    )].append(focus_row)
                excluded_families = {
                    str(value) for value in
                    options['exclude_structural_focus_family']
                }
                breakdown = current_scoring.get('breakdown') or {}
                ranked_families = sorted(
                    (
                        family for family in focuses_by_family
                        if Decimal(str(breakdown.get(
                            STRUCTURAL_FAMILY_SCORE_COMPONENT[family], 0,
                        ) or 0)) > 0
                    ),
                    key=lambda family: (
                        Decimal(str(breakdown.get(
                            STRUCTURAL_FAMILY_SCORE_COMPONENT[family], 0,
                        ) or 0)),
                        max(
                            row['penalty']
                            for row in focuses_by_family[family]
                        ),
                    ),
                    reverse=True,
                )
                selected_family = next(
                    (
                        family for family in ranked_families
                        if family not in excluded_families
                    ),
                    ranked_families[0] if ranked_families else None,
                )
                family_focuses = (
                    focuses_by_family[selected_family]
                    if selected_family else []
                )
                focus = (
                    reconstruction_rng.choice(
                        family_focuses[:min(12, len(family_focuses))]
                    )
                    if family_focuses else None
                )
                structural_reconstruction_attempt = {
                    'selected_family': selected_family,
                    'available_families': ranked_families,
                    'excluded_families': sorted(excluded_families),
                    'violation_focus': (focus or {}).get('violation_type'),
                    'accepted': False,
                    'score_delta': None,
                    'family_penalties': {
                        family: float(Decimal(str(breakdown.get(
                            STRUCTURAL_FAMILY_SCORE_COMPONENT[family], 0,
                        ) or 0)))
                        for family in ranked_families
                    },
                }
                schedule_start = min(instance.date for instance in instances)
                schedule_end = max(instance.date for instance in instances)
                window_days = reconstruction_rng.choice((42, 56, 70, 84))
                focus_dates = (focus or {}).get('dates') or []
                anchor = (
                    reconstruction_rng.choice(focus_dates)
                    if focus_dates
                    else reconstruction_rng.choice(instances).date
                )
                core_start = max(
                    schedule_start,
                    anchor - timedelta(days=reconstruction_rng.randrange(
                        0, max(window_days // 3, 1) + 1,
                    )),
                )
                core_end = min(
                    schedule_end,
                    core_start + timedelta(days=window_days - 1),
                )
                boundary_padding = _constraint_boundary_padding_days(contracts)
                focus_start = max(
                    schedule_start,
                    core_start - timedelta(days=boundary_padding),
                )
                focus_end = min(
                    schedule_end,
                    core_end + timedelta(days=boundary_padding),
                )
                deep_restart_number = max(
                    int(options['deep_restart_number']), 1,
                )
                temperature_ratio = max(
                    Decimal('0.04'),
                    Decimal('0.16')
                    - Decimal(deep_restart_number - 1) * Decimal('0.04'),
                )
                maximum_score_increase = max(
                    Decimal('5000'),
                    current_scoring['score'] * temperature_ratio,
                )
                reconstructed_state, reconstructed_scoring, reconstruction = (
                    _solve_bounded_multi_physician_neighborhood(
                        instances=instances,
                        physicians=physicians,
                        state=state,
                        scoring=current_scoring,
                        manual_pairs=manual_pairs,
                        targets=targets,
                        contract_by_physician=contracts,
                        requests_by_physician_date=requests_by_physician_date,
                        eligible_facilities_by_physician=(
                            eligible_facilities_by_physician
                        ),
                        minimum_rest_by_physician=minimum_rest_by_physician,
                        rng=reconstruction_rng,
                        focus_physician_ids=(focus or {}).get('physician_ids'),
                        focus_start=focus_start,
                        focus_end=focus_end,
                        cohort_size=reconstruction_rng.choice((14, 16, 18)),
                        allow_non_improving=True,
                        maximum_score_increase=maximum_score_increase,
                        diversification_bias=True,
                        time_limit_seconds=8.0,
                    )
                )
                reconstruction.update({
                    'violation_focus': (focus or {}).get('violation_type'),
                    'focus_start': focus_start.isoformat(),
                    'focus_end': focus_end.isoformat(),
                    'maximum_score_increase': float(maximum_score_increase),
                    'deep_restart_number': deep_restart_number,
                })
                structural_reconstruction_attempt.update({
                    'score_delta': (
                        float(
                            reconstructed_scoring['score']
                            - current_scoring['score']
                        )
                        if reconstruction.get('accepted') else None
                    ),
                })
                if reconstruction.get('accepted'):
                    reassignments = []
                    patch_is_balanced = True
                    for instance in instances:
                        before_owners = set(state.get(instance.id, ()))
                        after_owners = set(
                            reconstructed_state.get(instance.id, ())
                        )
                        removed = sorted(before_owners - after_owners)
                        added = sorted(after_owners - before_owners)
                        if len(removed) != len(added):
                            patch_is_balanced = False
                            break
                        reassignments.extend(
                            (instance.id, old_physician_id, new_physician_id)
                            for old_physician_id, new_physician_id in zip(
                                removed, added,
                            )
                        )
                    score_delta = (
                        reconstructed_scoring['score']
                        - current_scoring['score']
                    )
                    hard_invalids = any(
                        reconstructed_scoring['validation'].get(key, 0)
                        for key in (
                            'final_overlap_violations',
                            'final_rest_violations',
                            'final_duplicate_violations',
                            'final_overstaffed_violations',
                            'final_inactive_physician_violations',
                            'final_facility_ineligible_violations',
                        )
                    )
                    if (
                        patch_is_balanced
                        and reassignments
                        and not hard_invalids
                        and score_delta <= maximum_score_increase
                    ):
                        transition_validation = {
                            'operation': 'patch',
                            'source': 'structural_deep_reconstruction',
                            'reassignments': [
                                list(row) for row in reassignments
                            ],
                            'selection_mode': 'diversify',
                            'predicted_official_delta': float(score_delta),
                            'predicted_proportionality_delta': 0.0,
                            'authoritative_legal': True,
                            'authoritative_improving': score_delta < 0,
                            'authoritative_official_delta': float(score_delta),
                            'authoritative_proportionality_delta': None,
                            'accepted_for_diversification': True,
                            'score_source': 'authoritative_v1_tactic',
                            'reconstruction': reconstruction,
                        }
                        structural_reconstruction_attempt['accepted'] = True
            if transition_validation is None and best_vectorized_candidate is not None:
                transition_exact = evaluate_plateau_pairwise_swap(
                    instances=instances,
                    physicians=physicians,
                    state=state,
                    instances_by_id=instances_by_id,
                    manual_pairs=manual_pairs,
                    locked_open_instance_ids=locked_open_instance_ids,
                    targets=targets,
                    contract_by_physician=contracts,
                    requests_by_physician_date=requests_by_physician_date,
                    eligible_facilities_by_physician=eligible_facilities_by_physician,
                    minimum_rest_by_physician=minimum_rest_by_physician,
                    current_score=(
                        current_scoring['score'] if current_scoring is not None
                        else 0
                    ),
                    left_instance_id=best_vectorized_candidate['left_instance_id'],
                    left_physician_id=best_vectorized_candidate['left_physician_id'],
                    right_instance_id=best_vectorized_candidate['right_instance_id'],
                    right_physician_id=best_vectorized_candidate['right_physician_id'],
                    allow_proportional_tiebreak=True,
                    current_scoring=current_scoring,
                    legality_only=not score_checkpoint,
                )
                authoritative_legal = bool(transition_exact.get('legal'))
                if score_checkpoint:
                    authoritative_improving = bool(
                        transition_exact.get('improving')
                    )
                    authoritative_official_delta = (
                        float(transition_exact['score_delta'])
                        if transition_exact.get('score_delta') is not None
                        else None
                    )
                    authoritative_proportionality_delta = (
                        float(transition_exact['proportionality_delta'])
                        if transition_exact.get('proportionality_delta') is not None
                        else None
                    )
                    score_source = 'authoritative_checkpoint'
                else:
                    predicted_official_delta = float(
                        best_vectorized_candidate['predicted_official_delta']
                    )
                    predicted_proportionality_delta = float(
                        best_vectorized_candidate[
                            'predicted_proportionality_delta'
                        ]
                    )
                    authoritative_improving = authoritative_legal and (
                        predicted_official_delta < 0
                        or (
                            abs(predicted_official_delta) <= 0.0001
                            and predicted_proportionality_delta < 0
                        )
                    )
                    authoritative_official_delta = predicted_official_delta
                    authoritative_proportionality_delta = (
                        predicted_proportionality_delta
                    )
                    score_source = 'compiled_checkpointed'
                transition_validation = {
                    **best_vectorized_candidate,
                    'authoritative_legal': authoritative_legal,
                    'authoritative_improving': authoritative_improving,
                    'authoritative_official_delta': authoritative_official_delta,
                    'authoritative_proportionality_delta': (
                        authoritative_proportionality_delta
                    ),
                    'score_source': score_source,
                }
                diversification_delta = authoritative_official_delta
                transition_validation['accepted_for_diversification'] = bool(
                    best_vectorized_candidate['selection_mode'] == 'diversify'
                    and authoritative_legal
                    and diversification_delta is not None
                    and diversification_delta >= (
                        float(options['minimum_diversification_penalty'])
                        - 0.0001
                    )
                    and diversification_delta <= (
                        float(options['maximum_diversification_penalty'])
                        + 0.0001
                    )
                )
            elif reassignment_shortlist:
                best_reassignment = None
                for candidate in reassignment_shortlist:
                    exact = evaluate_plateau_reassignment(
                        instances=instances,
                        physicians=physicians,
                        state=state,
                        instances_by_id=instances_by_id,
                        manual_pairs=manual_pairs,
                        locked_open_instance_ids=locked_open_instance_ids,
                        targets=targets,
                        contract_by_physician=contracts,
                        requests_by_physician_date=requests_by_physician_date,
                        eligible_facilities_by_physician=(
                            eligible_facilities_by_physician
                        ),
                        minimum_rest_by_physician=minimum_rest_by_physician,
                        current_score=current_scoring['score'],
                        instance_id=candidate['instance_id'],
                        old_physician_id=candidate['old_physician_id'],
                        new_physician_id=candidate['new_physician_id'],
                        allow_proportional_tiebreak=True,
                        current_scoring=current_scoring,
                    )
                    if not exact.get('legal') or not exact.get('improving'):
                        continue
                    ranking = (
                        float(exact['score_delta']),
                        float(exact.get('proportionality_delta') or 0.0),
                    )
                    if (
                        best_reassignment is None
                        or ranking < best_reassignment[0]
                    ):
                        best_reassignment = (ranking, candidate, exact)
                if best_reassignment is not None:
                    _ranking, candidate, exact = best_reassignment
                    transition_validation = {
                        **candidate,
                        'selection_mode': 'improve',
                        'predicted_official_delta': float(
                            exact['score_delta']
                        ),
                        'predicted_proportionality_delta': float(
                            exact.get('proportionality_delta') or 0.0
                        ),
                        'authoritative_legal': True,
                        'authoritative_improving': True,
                        'authoritative_official_delta': float(
                            exact['score_delta']
                        ),
                        'authoritative_proportionality_delta': float(
                            exact.get('proportionality_delta') or 0.0
                        ),
                        'accepted_for_diversification': False,
                        'score_source': 'authoritative_v1_tactic',
                    }
            if transition_validation is None and cycle_shortlist:
                best_cycle = None
                for candidate in cycle_shortlist:
                    exact = evaluate_plateau_three_way_rotation(
                        instances=instances,
                        physicians=physicians,
                        state=state,
                        instances_by_id=instances_by_id,
                        manual_pairs=manual_pairs,
                        locked_open_instance_ids=locked_open_instance_ids,
                        targets=targets,
                        contract_by_physician=contracts,
                        requests_by_physician_date=requests_by_physician_date,
                        eligible_facilities_by_physician=(
                            eligible_facilities_by_physician
                        ),
                        minimum_rest_by_physician=minimum_rest_by_physician,
                        current_score=current_scoring['score'],
                        assignment_pairs=candidate['assignment_pairs'],
                        new_physician_ids=candidate['new_physician_ids'],
                    )
                    if not exact.get('legal') or not exact.get('improving'):
                        continue
                    ranking = float(exact['score_delta'])
                    if best_cycle is None or ranking < best_cycle[0]:
                        best_cycle = (ranking, candidate, exact)
                if best_cycle is not None:
                    _ranking, candidate, exact = best_cycle
                    transition_validation = {
                        **candidate,
                        'operation': 'rotate',
                        'assignment_pairs': [
                            list(pair) for pair in candidate['assignment_pairs']
                        ],
                        'new_physician_ids': list(
                            candidate['new_physician_ids']
                        ),
                        'selection_mode': 'improve',
                        'predicted_official_delta': float(
                            exact['score_delta']
                        ),
                        'predicted_proportionality_delta': 0.0,
                        'authoritative_legal': True,
                        'authoritative_improving': True,
                        'authoritative_official_delta': float(
                            exact['score_delta']
                        ),
                        'authoritative_proportionality_delta': None,
                        'accepted_for_diversification': False,
                        'score_source': 'authoritative_v1_tactic',
                    }
            if (
                transition_validation is None
                and best_selection is None
                and options['selection_mode'] == 'improve'
            ):
                reconstructed_state, reconstructed_scoring, reconstruction = (
                    _solve_bounded_multi_physician_neighborhood(
                        instances=instances,
                        physicians=physicians,
                        state=state,
                        scoring=current_scoring,
                        manual_pairs=manual_pairs,
                        targets=targets,
                        contract_by_physician=contracts,
                        requests_by_physician_date=requests_by_physician_date,
                        eligible_facilities_by_physician=(
                            eligible_facilities_by_physician
                        ),
                        minimum_rest_by_physician=minimum_rest_by_physician,
                        rng=random.Random(int(current_fingerprint[:16], 16)),
                        time_limit_seconds=4.0,
                    )
                )
                if reconstruction.get('accepted'):
                    reassignments = []
                    patch_is_balanced = True
                    for instance in instances:
                        before_owners = set(state.get(instance.id, ()))
                        after_owners = set(
                            reconstructed_state.get(instance.id, ())
                        )
                        removed = sorted(before_owners - after_owners)
                        added = sorted(after_owners - before_owners)
                        if len(removed) != len(added):
                            patch_is_balanced = False
                            break
                        reassignments.extend(
                            (instance.id, old_physician_id, new_physician_id)
                            for old_physician_id, new_physician_id in zip(
                                removed, added,
                            )
                        )
                    score_delta = (
                        reconstructed_scoring['score']
                        - current_scoring['score']
                    )
                    if patch_is_balanced and reassignments and score_delta < 0:
                        transition_validation = {
                            'operation': 'patch',
                            'source': 'multi_physician_reconstruction',
                            'reassignments': [
                                list(row) for row in reassignments
                            ],
                            'selection_mode': 'improve',
                            'predicted_official_delta': float(score_delta),
                            'predicted_proportionality_delta': 0.0,
                            'authoritative_legal': True,
                            'authoritative_improving': True,
                            'authoritative_official_delta': float(score_delta),
                            'authoritative_proportionality_delta': None,
                            'accepted_for_diversification': False,
                            'score_source': 'authoritative_v1_tactic',
                            'reconstruction': reconstruction,
                        }
            for sample in validation_sample:
                exact = evaluate_plateau_pairwise_swap(
                    instances=instances,
                    physicians=physicians,
                    state=state,
                    instances_by_id=instances_by_id,
                    manual_pairs=manual_pairs,
                    locked_open_instance_ids=locked_open_instance_ids,
                    targets=targets,
                    contract_by_physician=contracts,
                    requests_by_physician_date=requests_by_physician_date,
                    eligible_facilities_by_physician=eligible_facilities_by_physician,
                    minimum_rest_by_physician=minimum_rest_by_physician,
                    current_score=current_scoring['score'],
                    left_instance_id=sample['left_instance_id'],
                    left_physician_id=sample['left_physician_id'],
                    right_instance_id=sample['right_instance_id'],
                    right_physician_id=sample['right_physician_id'],
                    allow_proportional_tiebreak=True,
                    current_scoring=current_scoring,
                )
                if not exact.get('legal'):
                    legal_mismatches += 1
                    continue
                selected_ids = {
                    sample['left_physician_id'], sample['right_physician_id'],
                }
                before_components = _selected_physician_score(
                    instances,
                    physicians,
                    selected_ids,
                    state,
                    targets,
                    contracts,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    return_components=True,
                )
                trial_state = _copy_state(state)
                _replace_in_state(
                    trial_state,
                    sample['left_instance_id'],
                    sample['left_physician_id'],
                    sample['right_physician_id'],
                )
                _replace_in_state(
                    trial_state,
                    sample['right_instance_id'],
                    sample['right_physician_id'],
                    sample['left_physician_id'],
                )
                after_components = _selected_physician_score(
                    instances,
                    physicians,
                    selected_ids,
                    trial_state,
                    targets,
                    contracts,
                    requests_by_physician_date,
                    eligible_facilities_by_physician,
                    minimum_rest_by_physician,
                    return_components=True,
                )
                score_delta = exact.get('score_delta')
                proportionality_delta = exact.get('proportionality_delta')
                component_deltas = {
                    key: float(after_components[key] - before_components[key])
                    for key in before_components
                    if key != 'total_score'
                }
                if abs(
                    sum(component_deltas.values()) - float(score_delta)
                ) > 0.0001:
                    component_delta_mismatches += 1
                if abs(
                    component_deltas['workload_score']
                    - sample['predicted_workload_delta']
                ) > 0.0001:
                    workload_delta_mismatches += 1
                if abs(
                    component_deltas['weekend_score']
                    - sample['predicted_weekend_delta']
                ) > 0.0001:
                    weekend_delta_mismatches += 1
                if abs(
                    component_deltas['night_score']
                    - sample['predicted_night_delta']
                ) > 0.0001:
                    night_delta_mismatches += 1
                if abs(
                    component_deltas['request_score']
                    - sample['predicted_request_delta']
                ) > 0.0001:
                    request_delta_mismatches += 1
                if abs(
                    component_deltas['consecutive_days_score']
                    - sample['predicted_consecutive_delta']
                ) > 0.0001:
                    consecutive_delta_mismatches += 1
                if abs(
                    component_deltas['same_shift_score']
                    - sample['predicted_same_shift_delta']
                ) > 0.0001:
                    same_shift_delta_mismatches += 1
                if abs(
                    component_deltas['shift_rule_score']
                    - sample['predicted_shift_rule_delta']
                ) > 0.0001:
                    shift_rule_delta_mismatches += 1
                predicted_official_delta = sum((
                    sample['predicted_workload_delta'],
                    sample['predicted_weekend_delta'],
                    sample['predicted_night_delta'],
                    sample['predicted_request_delta'],
                    sample['predicted_consecutive_delta'],
                    sample['predicted_same_shift_delta'],
                    sample['predicted_shift_rule_delta'],
                ))
                if abs(predicted_official_delta - float(score_delta)) > 0.0001:
                    official_delta_mismatches += 1
                if any(
                    abs(component_deltas[key]) > 0.0001
                    for key in (
                        'underutilization_score', 'validation_score',
                    )
                ):
                    excluded_component_nonzero_candidates += 1
                for key, delta in component_deltas.items():
                    if abs(delta) <= 1e-12:
                        continue
                    stats = component_delta_stats[key]
                    stats['nonzero_candidates'] += 1
                    stats['absolute_delta_total'] += abs(delta)
                    if delta > 0:
                        stats['positive_candidates'] += 1
                        stats['maximum_positive_delta'] = max(
                            stats['maximum_positive_delta'], delta,
                        )
                    else:
                        stats['negative_candidates'] += 1
                        stats['minimum_negative_delta'] = min(
                            stats['minimum_negative_delta'], delta,
                        )
                if score_delta == 0:
                    score_neutral_candidates += 1
                    exact_improves = (
                        proportionality_delta is not None
                        and proportionality_delta < 0
                    )
                    predicted_improves = sample['predicted_priority_delta'] < 0
                    if exact_improves == predicted_improves:
                        proportionality_direction_matches += 1
                    else:
                        proportionality_direction_mismatches += 1
                exact_results.append({
                    'score_delta': float(score_delta) if score_delta is not None else None,
                    'proportionality_delta': (
                        float(proportionality_delta)
                        if proportionality_delta is not None else None
                    ),
                })
        if (
            transition_validation is not None
            and transition_validation.get('operation') == 'patch'
            and any(
                physician_id not in physician_index
                for _instance_id, old_physician_id, new_physician_id
                in transition_validation.get('reassignments', ())
                for physician_id in (old_physician_id, new_physician_id)
            )
        ):
            transition_validation = None
        if (
            transition_validation is not None
            and transition_validation.get('authoritative_legal')
            and (
                transition_validation.get('authoritative_improving')
                or transition_validation.get('accepted_for_diversification')
            )
        ):
            transition_refresh_started = perf_counter()
            is_reassignment = (
                transition_validation.get('operation') == 'reassign'
            )
            is_rotation = (
                transition_validation.get('operation') == 'rotate'
            )
            is_patch = transition_validation.get('operation') == 'patch'
            if is_patch:
                patch_reassignments = [
                    tuple(row) for row in transition_validation['reassignments']
                ]
                next_state = reassign_assignments(
                    state, patch_reassignments,
                )
                affected_ids = tuple(sorted({
                    physician_id
                    for _instance_id, old_physician_id, new_physician_id
                    in patch_reassignments
                    for physician_id in (
                        old_physician_id, new_physician_id,
                    )
                }))
            elif is_rotation:
                assignment_pairs = [
                    tuple(pair)
                    for pair in transition_validation['assignment_pairs']
                ]
                new_physician_ids = tuple(
                    transition_validation['new_physician_ids']
                )
                next_state = rotate_assignments(
                    state, assignment_pairs, new_physician_ids,
                )
                affected_ids = tuple(pair[1] for pair in assignment_pairs)
            elif is_reassignment:
                next_state = reassign_assignment(
                    state,
                    transition_validation['instance_id'],
                    transition_validation['old_physician_id'],
                    transition_validation['new_physician_id'],
                )
                affected_ids = (
                    transition_validation['old_physician_id'],
                    transition_validation['new_physician_id'],
                )
            else:
                next_state = swap_assignments(
                    state,
                    (
                        transition_validation['left_instance_id'],
                        transition_validation['left_physician_id'],
                    ),
                    (
                        transition_validation['right_instance_id'],
                        transition_validation['right_physician_id'],
                    ),
                )
                affected_ids = (
                    transition_validation['left_physician_id'],
                    transition_validation['right_physician_id'],
                )
            next_pairs = sorted({
                (instance_id, physician_id)
                for instance_id, owners in next_state.items()
                for physician_id in owners
                if physician_id in physician_index
                and (instance_id, physician_id) not in manual_pairs
            })
            refreshed_pairs, refreshed_tables, refresh_breakdown = (
                prepare_affected_assignment_tables(
                    next_state, affected_ids, next_pairs,
                )
            )
            if is_patch:
                next_engine_context = engine_context.apply_reassignment_patch(
                    patch_reassignments,
                    next_pairs,
                    refreshed_pairs,
                    refreshed_tables,
                )
            elif is_rotation:
                next_engine_context = engine_context.apply_rotation(
                    assignment_pairs,
                    new_physician_ids,
                    next_pairs,
                    refreshed_pairs,
                    refreshed_tables,
                )
            elif is_reassignment:
                next_engine_context = engine_context.apply_reassignment(
                    transition_validation['instance_id'],
                    transition_validation['old_physician_id'],
                    transition_validation['new_physician_id'],
                    next_pairs,
                    refreshed_pairs,
                    refreshed_tables,
                )
            else:
                next_engine_context = engine_context.apply_selection(
                    best_selection,
                    next_pairs,
                    refreshed_pairs,
                    refreshed_tables,
                )
            expected_fingerprint = schedule_fingerprint(next_state)
            next_operations = tuple(
                ('R', instance_id, old_physician_id, new_physician_id)
                for instance_id, old_physician_id, new_physician_id
                in patch_reassignments
            ) if is_patch else ((
                tuple(
                    ['C']
                    + [
                        value
                        for pair, new_physician_id in zip(
                            assignment_pairs, new_physician_ids,
                        )
                        for value in (
                            pair[0], pair[1], new_physician_id,
                        )
                    ]
                )
                if is_rotation else (
                    'R', transition_validation['instance_id'],
                    transition_validation['old_physician_id'],
                    transition_validation['new_physician_id'],
                )
                if is_reassignment else (
                    'S', transition_validation['left_instance_id'],
                    transition_validation['left_physician_id'],
                    transition_validation['right_instance_id'],
                    transition_validation['right_physician_id'],
                )
            ),)
            next_swap_lineage = swap_lineage + next_operations
            _ENGINE_CONTEXT_CACHE.clear()
            _ENGINE_CONTEXT_CACHE[(
                run.id,
                stress_contract_count,
                next_swap_lineage,
                next_engine_context.fingerprint,
            )] = next_engine_context
            in_process_transition = {
                'refresh_seconds': (
                    perf_counter() - transition_refresh_started
                ),
                'refresh_breakdown': refresh_breakdown,
                'next_schedule_fingerprint': next_engine_context.fingerprint,
                'expected_schedule_fingerprint': expected_fingerprint,
                'fingerprint_matches': (
                    next_engine_context.fingerprint == expected_fingerprint
                ),
                'accepted_transitions': (
                    next_engine_context.accepted_transitions
                ),
                'evaluated_schedules': next_engine_context.evaluated_schedules,
                'seen_fingerprints': len(next_engine_context.seen_fingerprints),
            }
        validation_seconds = perf_counter() - validation_started
        exact_rate = (
            len(validation_sample) / validation_seconds
            if validation_seconds and validation_sample else 0.0
        )
        result = {
            'stage': 'V2_SWAP_KERNEL_STAGE_SEVEN_SHIFT_RULES',
            'read_only': True,
            'applied_in_memory_swaps': applied_in_memory_swaps,
            'run_number': run.run_number,
            'schedule_version_id': version.id,
            'schedule_fingerprint': engine_context.fingerprint,
            'authoritative_current_score': authoritative_current_score,
            'engine_context_cache_hit': engine_context_cache_hit,
            'engine_context_cache_entries': len(_ENGINE_CONTEXT_CACHE),
            'engine_context_evaluated_schedules': (
                engine_context.evaluated_schedules
            ),
            'engine_context_accepted_transitions': (
                engine_context.accepted_transitions
            ),
            'stress_contract_count': stress_contract_count,
            'stress_all_templates_governed': bool(stress_contract_count),
            'movable_assignments': assignment_count,
            'reassignment_candidate_schedules': (
                reassignment_candidate_schedules
            ),
            'distinct_candidate_schedules': distinct_states,
            'hard_feasible_schedules': hard_feasible_states,
            'distribution_scored_schedules': scored_states,
            'distribution_improving_schedules': improving_states,
            'workload_neutral_schedules': workload_neutral_states,
            'workload_nonworsening_distribution_improvements': (
                workload_nonworsening_improving_states
            ),
            'weekend_neutral_schedules': weekend_neutral_states,
            'workload_weekend_nonworsening_distribution_improvements': (
                workload_weekend_nonworsening_improving_states
            ),
            'night_neutral_schedules': night_neutral_states,
            'request_neutral_schedules': request_neutral_states,
            'consecutive_day_neutral_schedules': consecutive_neutral_states,
            'same_shift_neutral_schedules': same_shift_neutral_states,
            'completed_components_nonworsening_distribution_improvements': (
                completed_components_nonworsening_improving_states
            ),
            'elapsed_seconds': elapsed,
            'preparation_seconds': preparation_seconds,
            'preparation_breakdown': preparation_breakdown,
            'affected_physician_refresh': affected_refresh,
            'total_seconds': preparation_seconds + elapsed,
            'distinct_schedules_per_second': rate,
            'target_schedules_per_second': target,
            'meets_stage_one_rate': rate >= target,
            'authoritative_validation': {
                'sample_size': len(validation_sample),
                'elapsed_seconds': validation_seconds,
                'candidates_per_second': exact_rate,
                'legal_mismatches': legal_mismatches,
                'score_neutral_candidates': score_neutral_candidates,
                'proportionality_direction_matches': proportionality_direction_matches,
                'proportionality_direction_mismatches': proportionality_direction_mismatches,
                'component_delta_mismatches': component_delta_mismatches,
                'workload_delta_mismatches': workload_delta_mismatches,
                'weekend_delta_mismatches': weekend_delta_mismatches,
                'night_delta_mismatches': night_delta_mismatches,
                'request_delta_mismatches': request_delta_mismatches,
                'consecutive_delta_mismatches': consecutive_delta_mismatches,
                'same_shift_delta_mismatches': same_shift_delta_mismatches,
                'shift_rule_delta_mismatches': shift_rule_delta_mismatches,
                'official_delta_mismatches': official_delta_mismatches,
                'excluded_component_nonzero_candidates': (
                    excluded_component_nonzero_candidates
                ),
                'component_delta_stats': dict(component_delta_stats),
                'score_delta_min': (
                    min(row['score_delta'] for row in exact_results if row['score_delta'] is not None)
                    if any(row['score_delta'] is not None for row in exact_results) else None
                ),
                'score_delta_max': (
                    max(row['score_delta'] for row in exact_results if row['score_delta'] is not None)
                    if any(row['score_delta'] is not None for row in exact_results) else None
                ),
            },
            'best_candidate_transition_validation': transition_validation,
            'structural_reconstruction_attempt': (
                structural_reconstruction_attempt
            ),
            'in_process_transition': in_process_transition,
            'scope': (
                'Vectorized fixed-state pair swaps with facility eligibility, '
                'duplicate assignment, overlap/rest screening, exact workload, '
                'shift-group, weekend, night, request, consecutive-day, and '
                'same-shift penalty deltas, and normalized facility/time '
                'proportionality deltas for unguided shifts. Pair-swap local '
                'optima also expose vectorized hard-feasible one-way '
                'reassignments directed by V1 request, workload, weekend, '
                'same-shift, consecutive-day, and night violations, followed '
                'by weekend cycles and bounded atomic cohort reconstruction.'
            ),
            'not_yet_proven': (
                'The native V1 tactic portfolio is integrated. Additional '
                'completed-run and fresh-fill validation is still required '
                'before declaring production parity.'
            ),
        }
        if options['as_json']:
            self.stdout.write(json.dumps(result, sort_keys=True))
            return
        for key, value in result.items():
            self.stdout.write(f'{key}: {value}')
