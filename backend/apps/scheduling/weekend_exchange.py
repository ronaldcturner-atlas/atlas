from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from time import monotonic

from ortools.sat.python import cp_model

from . import optimizer


SCORE_SCALE = 60


def _model_or(model, values, name):
    if any(isinstance(value, int) and value == 1 for value in values):
        return 1
    variables = [value for value in values if not isinstance(value, int)]
    if not variables:
        return 0
    result = model.new_bool_var(name)
    for value in variables:
        model.add(result >= value)
    model.add(result <= sum(variables))
    return result


def _model_and(model, left, right, name):
    if isinstance(left, int):
        return right if left else 0
    if isinstance(right, int):
        return left if right else 0
    result = model.new_bool_var(name)
    model.add(result <= left)
    model.add(result <= right)
    model.add(result >= left + right - 1)
    return result


def _calendar_days(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _add_weekend_objective(
    model,
    *,
    instances,
    state,
    contracts,
    variables,
    movable,
    fixed_by_physician,
    block_start,
    block_end,
):
    cohort_ids = sorted(contracts)
    movable_by_physician_instance = defaultdict(list)
    for slot, (instance, _owner_id) in enumerate(movable):
        for physician_id in cohort_ids:
            variable = variables.get((slot, physician_id))
            if variable is not None:
                movable_by_physician_instance[
                    physician_id, instance.id
                ].append(variable)
    boundary = optimizer._published_boundary_context(instances)
    prior_range = getattr(
        instances[0], '_published_boundary_date_range', None,
    ) if instances else None
    objective_terms = []
    for physician_id in cohort_ids:
        settings = contracts[physician_id].weekend_settings
        settings = settings if isinstance(settings, dict) else {}
        prior_weekend = [
            instance for instance in boundary.get(physician_id, ())
            if optimizer._is_weekend_designated(instance)
        ]
        first_context_day = min(
            [block_start, *[instance.date for instance in prior_weekend]],
        )
        calendar_days = list(_calendar_days(first_context_day, block_end))
        fixed_weekend_dates = {
            instance.date
            for instance in fixed_by_physician[physician_id]
            if optimizer._is_weekend_designated(instance)
        }
        prior_weekend_dates = {instance.date for instance in prior_weekend}
        weekend_by_date = {}
        current_weekend_by_date = {}
        for day in calendar_days:
            current_values = [
                variable
                for instance in instances
                if instance.date == day
                and optimizer._is_weekend_designated(instance)
                for variable in movable_by_physician_instance.get(
                    (physician_id, instance.id), ()
                )
            ]
            current_weekend_by_date[day] = (
                1 if day in fixed_weekend_dates else _model_or(
                    model,
                    current_values,
                    f'current_weekend_day_{physician_id}_{day.isoformat()}',
                )
            )
            if day in prior_weekend_dates or day in fixed_weekend_dates:
                weekend_by_date[day] = 1
            else:
                weekend_by_date[day] = _model_or(
                    model,
                    current_values,
                    f'weekend_day_{physician_id}_{day.isoformat()}',
                )

        for rule in settings.get('period_rules') or []:
            if not isinstance(rule, dict):
                continue
            period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
            for window_start, window_end in optimizer._period_windows(
                instances, period_type,
            ):
                fixed_count = sum(
                    1
                    for instance in fixed_by_physician[physician_id]
                    if window_start <= instance.date <= window_end
                    and optimizer._is_weekend_designated(instance)
                )
                terms = [
                    variable
                    for instance in instances
                    if window_start <= instance.date <= window_end
                    and optimizer._is_weekend_designated(instance)
                    for variable in movable_by_physician_instance.get(
                        (physician_id, instance.id), ()
                    )
                ]
                total = fixed_count + sum(terms)
                maximum_possible = fixed_count + len(terms)
                for side in ('min', 'max'):
                    limit = optimizer._decimal_or_none(
                        rule.get(f'{side}_volume')
                    )
                    weight = optimizer._decimal_or_none(
                        rule.get(f'{side}_penalty_weight')
                    )
                    if limit is None or weight is None or weight <= 0:
                        continue
                    deviation = model.new_int_var(
                        0,
                        max(maximum_possible, int(limit), 1),
                        f'weekend_{side}_{physician_id}_{window_start.isoformat()}',
                    )
                    if side == 'min':
                        model.add(deviation >= int(limit) - total)
                    else:
                        model.add(deviation >= total - int(limit))
                    objective_terms.append(
                        int(weight * SCORE_SCALE) * deviation
                    )

        minimum_shifts = optimizer._configured_positive_int(
            settings, 'min_consecutive_weekend_shifts',
        )
        minimum_shift_weight = optimizer._configured_positive_penalty(
            settings,
            'min_consecutive_weekend_shifts_penalty_weight',
            Decimal('0'),
        )
        maximum_shifts = optimizer._configured_positive_int(
            settings, 'max_consecutive_weekend_shifts',
        )
        maximum_shift_weight = optimizer._configured_positive_penalty(
            settings,
            'max_consecutive_weekend_shifts_penalty_weight',
            Decimal('0'),
        )
        if minimum_shifts and minimum_shift_weight > 0:
            for day in calendar_days:
                current = weekend_by_date.get(day, 0)
                previous = weekend_by_date.get(day - timedelta(days=1), 0)
                block_begin = _model_and(
                    model,
                    current,
                    1 - previous,
                    f'weekend_shift_start_{physician_id}_{day.isoformat()}',
                )
                complete = block_begin
                for offset in range(1, minimum_shifts):
                    complete = _model_and(
                        model,
                        complete,
                        weekend_by_date.get(
                            day + timedelta(days=offset), 0,
                        ),
                        f'weekend_shift_complete_{physician_id}_{day.isoformat()}_{offset}',
                    )
                short = _model_and(
                    model,
                    block_begin,
                    1 - complete,
                    f'weekend_shift_short_{physician_id}_{day.isoformat()}',
                )
                has_current = _model_or(
                    model,
                    [
                        current_weekend_by_date.get(
                            day + timedelta(days=offset), 0,
                        )
                        for offset in range(minimum_shifts)
                    ],
                    f'weekend_shift_current_{physician_id}_{day.isoformat()}',
                )
                scored_short = _model_and(
                    model,
                    short,
                    has_current,
                    f'weekend_shift_scored_{physician_id}_{day.isoformat()}',
                )
                objective_terms.append(
                    int(minimum_shift_weight * SCORE_SCALE) * scored_short
                )
        if maximum_shifts and maximum_shift_weight > 0:
            for start_index, start_day in enumerate(calendar_days):
                window = calendar_days[
                    start_index:start_index + maximum_shifts + 1
                ]
                if len(window) < maximum_shifts + 1:
                    continue
                current = weekend_by_date.get(start_day, 0)
                previous = weekend_by_date.get(
                    start_day - timedelta(days=1), 0,
                )
                block_begin = _model_and(
                    model,
                    current,
                    1 - previous,
                    f'weekend_shift_max_start_{physician_id}_{start_index}',
                )
                values = [
                    block_begin,
                    *[weekend_by_date.get(day, 0) for day in window[1:]],
                ]
                if any(
                    isinstance(value, int) and value == 0 for value in values
                ):
                    continue
                if all(
                    isinstance(value, int) and value == 1 for value in values
                ):
                    objective_terms.append(
                        int(maximum_shift_weight * SCORE_SCALE)
                    )
                    continue
                excess = model.new_bool_var(
                    f'weekend_shift_excess_{physician_id}_{start_index}'
                )
                for value in values:
                    model.add(excess <= value)
                model.add(excess >= sum(values) - len(values) + 1)
                objective_terms.append(
                    int(maximum_shift_weight * SCORE_SCALE) * excess
                )

        weekend_starts = sorted({
            optimizer._weekend_anchor(day) for day in calendar_days
        })
        worked_weekend = {
            weekend_start: _model_or(
                model,
                [
                    weekend_by_date.get(
                        weekend_start + timedelta(days=offset), 0,
                    )
                    for offset in range(7)
                ],
                f'worked_weekend_{physician_id}_{weekend_start.isoformat()}',
            )
            for weekend_start in weekend_starts
        }
        current_worked_weekend = {
            weekend_start: _model_or(
                model,
                [
                    current_weekend_by_date.get(
                        weekend_start + timedelta(days=offset), 0,
                    )
                    for offset in range(7)
                ],
                f'current_worked_weekend_{physician_id}_{weekend_start.isoformat()}',
            )
            for weekend_start in weekend_starts
        }
        minimum_weekends = optimizer._configured_positive_int(
            settings, 'min_consecutive_weekends',
        )
        minimum_weekends_weight = optimizer._configured_positive_penalty(
            settings,
            'min_consecutive_weekends_penalty_weight',
            Decimal('0'),
        )
        maximum_weekends = optimizer._configured_positive_int(
            settings, 'max_consecutive_weekends',
        )
        maximum_weekends_weight = optimizer._configured_positive_penalty(
            settings,
            'max_consecutive_weekends_penalty_weight',
            Decimal('0'),
        )
        if minimum_weekends and minimum_weekends_weight > 0:
            for weekend_start in weekend_starts:
                current = worked_weekend[weekend_start]
                previous = worked_weekend.get(
                    weekend_start - timedelta(days=7), 0,
                )
                streak_start = _model_and(
                    model,
                    current,
                    1 - previous,
                    f'weekend_streak_start_{physician_id}_{weekend_start.isoformat()}',
                )
                complete = streak_start
                for offset in range(1, minimum_weekends):
                    complete = _model_and(
                        model,
                        complete,
                        worked_weekend.get(
                            weekend_start + timedelta(days=7 * offset), 0,
                        ),
                        f'weekend_streak_complete_{physician_id}_{weekend_start.isoformat()}_{offset}',
                    )
                short = _model_and(
                    model,
                    streak_start,
                    1 - complete,
                    f'weekend_streak_short_{physician_id}_{weekend_start.isoformat()}',
                )
                has_current = _model_or(
                    model,
                    [
                        current_worked_weekend.get(
                            weekend_start + timedelta(days=7 * offset), 0,
                        )
                        for offset in range(minimum_weekends)
                    ],
                    f'weekend_streak_current_{physician_id}_{weekend_start.isoformat()}',
                )
                scored_short = _model_and(
                    model,
                    short,
                    has_current,
                    f'weekend_streak_scored_{physician_id}_{weekend_start.isoformat()}',
                )
                objective_terms.append(
                    int(minimum_weekends_weight * SCORE_SCALE) * scored_short
                )
        if maximum_weekends and maximum_weekends_weight > 0:
            for start_index in range(
                max(len(weekend_starts) - maximum_weekends, 0)
            ):
                window = weekend_starts[
                    start_index:start_index + maximum_weekends + 1
                ]
                values = [worked_weekend[start] for start in window]
                if any(
                    isinstance(value, int) and value == 0 for value in values
                ):
                    continue
                if all(
                    isinstance(value, int) and value == 1 for value in values
                ):
                    objective_terms.append(
                        int(maximum_weekends_weight * SCORE_SCALE)
                    )
                    continue
                excess = model.new_bool_var(
                    f'weekend_streak_excess_{physician_id}_{start_index}'
                )
                for value in values:
                    model.add(excess <= value)
                model.add(excess >= sum(values) - len(values) + 1)
                objective_terms.append(
                    int(maximum_weekends_weight * SCORE_SCALE) * excess
                )

        friday_weight = optimizer._configured_positive_penalty(
            settings,
            'block_friday_night_before_weekend_off_penalty_weight',
            Decimal('0'),
        )
        if (
            settings.get('block_friday_night_before_weekend_off')
            and friday_weight > 0
        ):
            prior_friday_nights = {
                instance.date
                for instance in boundary.get(physician_id, ())
                if instance.date.weekday() == 4
                and instance.shift_template.night_shift
            }
            fixed_friday_nights = {
                instance.date
                for instance in fixed_by_physician[physician_id]
                if instance.date.weekday() == 4
                and instance.shift_template.night_shift
            }
            friday_start = prior_range[0] if prior_range else block_start
            for friday in (
                day for day in _calendar_days(friday_start, block_end)
                if day.weekday() == 4
            ):
                saturday = friday + timedelta(days=1)
                sunday = friday + timedelta(days=2)
                if sunday < block_start or saturday > block_end:
                    continue
                if any(
                    not (
                        block_start <= day <= block_end
                        or (
                            prior_range is not None
                            and prior_range[0] <= day <= prior_range[1]
                        )
                    )
                    for day in (saturday, sunday)
                ):
                    continue
                friday_night = int(
                    friday in prior_friday_nights
                    or friday in fixed_friday_nights
                )
                weekend_work = _model_or(
                    model,
                    [
                        weekend_by_date.get(saturday, 0),
                        weekend_by_date.get(sunday, 0),
                    ],
                    f'weekend_work_{physician_id}_{friday.isoformat()}',
                )
                violation = _model_and(
                    model,
                    friday_night,
                    1 - weekend_work,
                    f'friday_before_weekend_off_{physician_id}_{friday.isoformat()}',
                )
                objective_terms.append(
                    int(friday_weight * SCORE_SCALE) * violation
                )
    return objective_terms


def solve_balanced_weekend_streak_exchange(
    *,
    instances,
    physicians,
    state,
    scoring,
    manual_pairs,
    targets,
    contract_by_physician,
    requests_by_physician_date,
    eligible_facilities_by_physician,
    minimum_rest_by_physician,
    year,
    month,
    time_limit_seconds=4,
    maximum_reassignments=24,
    rng,
    clock=monotonic,
):
    """Try one balanced monthly exchange for overlong weekend streaks.

    Night ownership is fixed. Each physician keeps the same assignment count
    for every facility/template/month signature, so workload, facility, shift
    type, and night-volume capacity cannot drift. The authoritative complete
    scorer remains the only acceptance rule.
    """
    started = clock()
    deadline = started + max(float(time_limit_seconds), 0.1)
    debug = {
        'accepted': False,
        'attempts': 0,
        'candidates': 0,
        'month': f'{int(year):04d}-{int(month):02d}',
        'maximum_reassignments': int(maximum_reassignments),
        'preserve_capacity_signatures': True,
        'freeze_night_assignments': True,
    }

    def expired(stage):
        if clock() < deadline:
            return False
        debug['stopped'] = True
        debug['stopped_stage'] = stage
        return True

    cohort_ids = {
        physician.id
        for physician in physicians
        if not getattr(
            contract_by_physician.get(physician.id),
            'manual_assignment_only',
            False,
        )
    }
    allowed_instance_ids = {
        instance.id
        for instance in instances
        if (instance.date.year, instance.date.month) == (year, month)
        and optimizer._is_weekend_designated(instance)
        and not instance.shift_template.night_shift
        and not getattr(instance, 'is_locked_open', False)
    }
    movable = [
        (instance, physician_id)
        for instance in instances
        if instance.id in allowed_instance_ids
        for physician_id in state[instance.id]
        if physician_id in cohort_ids
        and (instance.id, physician_id) not in manual_pairs
    ]
    if len(movable) < 2 or len(movable) > 350:
        debug['skipped_reason'] = 'neighborhood_size'
        return optimizer._copy_state(state), scoring, debug
    active_ids = {owner_id for _instance, owner_id in movable}
    cohort_ids.intersection_update(active_ids)
    debug['physician_ids'] = sorted(cohort_ids)
    debug['movable_assignments'] = len(movable)

    movable_pairs = {
        (instance.id, physician_id)
        for instance, physician_id in movable
    }
    fixed_indexes = {
        physician_id: optimizer._AssignmentIntervalIndex()
        for physician_id in cohort_ids
    }
    fixed_by_physician = defaultdict(list)
    for instance in instances:
        for physician_id in state[instance.id]:
            if (
                physician_id in cohort_ids
                and (instance.id, physician_id) not in movable_pairs
            ):
                fixed_indexes[physician_id].append(
                    (instance.start_datetime, instance.end_datetime)
                )
                fixed_by_physician[physician_id].append(instance)

    model = cp_model.CpModel()
    variables = {}
    slot_variables = defaultdict(list)
    request_terms = []
    for slot, (instance, _owner_id) in enumerate(movable):
        if expired('create_assignment_variables'):
            return optimizer._copy_state(state), scoring, debug
        for physician_id in cohort_ids:
            if not optimizer._is_instance_eligible(
                instance,
                physician_id,
                eligible_facilities_by_physician,
            ):
                continue
            if fixed_indexes[physician_id].conflicts(
                instance,
                minimum_rest_by_physician[physician_id],
            ):
                continue
            variable = model.new_bool_var(f'x_{slot}_{physician_id}')
            variables[slot, physician_id] = variable
            slot_variables[slot].append(variable)
            contract = contract_by_physician.get(physician_id)
            if contract is not None:
                request_score, _violations, _rewards = optimizer._request_score(
                    optimizer._requests_for_shift(
                        requests_by_physician_date,
                        physician_id,
                        instance,
                    ),
                    contract,
                )
                if request_score:
                    request_terms.append(
                        int(Decimal(request_score) * SCORE_SCALE) * variable
                    )
        if not slot_variables[slot]:
            debug['skipped_reason'] = 'no_candidate_for_slot'
            return optimizer._copy_state(state), scoring, debug
        model.add_exactly_one(slot_variables[slot])

    slots_by_instance = defaultdict(list)
    for slot, (instance, _owner_id) in enumerate(movable):
        slots_by_instance[instance.id].append(slot)
    for slots in slots_by_instance.values():
        for physician_id in cohort_ids:
            same_instance = [
                variables[slot, physician_id]
                for slot in slots
                if (slot, physician_id) in variables
            ]
            if len(same_instance) > 1:
                model.add_at_most_one(same_instance)

    signatures = sorted({
        (instance.facility_id, instance.shift_template_id)
        for instance, _owner_id in movable
    })
    for physician_id in cohort_ids:
        for signature in signatures:
            baseline_count = sum(
                owner_id == physician_id
                and (instance.facility_id, instance.shift_template_id)
                == signature
                for instance, owner_id in movable
            )
            terms = [
                variables[slot, physician_id]
                for slot, (instance, _owner_id) in enumerate(movable)
                if (slot, physician_id) in variables
                and (instance.facility_id, instance.shift_template_id)
                == signature
            ]
            if terms or baseline_count:
                model.add(sum(terms) == baseline_count)

    for left_slot, (left, _left_owner) in enumerate(movable):
        if expired('create_pair_constraints'):
            return optimizer._copy_state(state), scoring, debug
        for right_slot in range(left_slot + 1, len(movable)):
            right = movable[right_slot][0]
            if left.id == right.id:
                continue
            for physician_id in cohort_ids:
                left_variable = variables.get((left_slot, physician_id))
                right_variable = variables.get((right_slot, physician_id))
                if left_variable is None or right_variable is None:
                    continue
                index = optimizer._AssignmentIntervalIndex()
                index.append((left.start_datetime, left.end_datetime))
                if index.conflicts(
                    right,
                    minimum_rest_by_physician[physician_id],
                ):
                    model.add(left_variable + right_variable <= 1)

    changed = []
    for slot, (_instance, owner_id) in enumerate(movable):
        owner_variable = variables.get((slot, owner_id))
        changed.append(1 if owner_variable is None else 1 - owner_variable)
        for physician_id in cohort_ids:
            variable = variables.get((slot, physician_id))
            if variable is not None:
                model.add_hint(variable, int(physician_id == owner_id))
    model.add(sum(changed) <= max(int(maximum_reassignments), 0))

    block = getattr(instances[0], 'schedule_block', None) if instances else None
    block_start = (
        block.start_date if block is not None
        else min(instance.date for instance in instances)
    )
    block_end = (
        block.end_date if block is not None
        else max(instance.date for instance in instances)
    )
    weekend_terms = _add_weekend_objective(
        model,
        instances=instances,
        state=state,
        contracts={
            physician_id: contract_by_physician[physician_id]
            for physician_id in cohort_ids
        },
        variables=variables,
        movable=movable,
        fixed_by_physician=fixed_by_physician,
        block_start=block_start,
        block_end=block_end,
    )

    if not weekend_terms:
        debug['skipped_reason'] = 'no_weekend_rules'
        return optimizer._copy_state(state), scoring, debug
    stability_terms = [
        rng.randint(1, 7) * change for change in changed
    ]
    model.minimize(
        sum(weekend_terms) + sum(request_terms) + sum(stability_terms)
    )
    remaining = deadline - clock()
    if remaining < 0.1:
        debug['stopped'] = True
        debug['stopped_stage'] = 'before_solver'
        return optimizer._copy_state(state), scoring, debug

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(remaining, 0.1)
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = rng.randrange(1, 2_000_000_000)
    debug['solver_workers'] = solver.parameters.num_search_workers
    status = solver.solve(model)
    debug['attempts'] = 1
    debug['solver_status'] = solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return optimizer._copy_state(state), scoring, debug

    trial = optimizer._copy_state(state)
    for instance, owner_id in movable:
        trial[instance.id].remove(owner_id)
    for slot, (instance, _owner_id) in enumerate(movable):
        for physician_id in cohort_ids:
            variable = variables.get((slot, physician_id))
            if variable is not None and solver.boolean_value(variable):
                trial[instance.id].append(physician_id)
                break
    trial_scoring = optimizer._score_schedule(
        instances,
        physicians,
        trial,
        targets,
        contract_by_physician,
        requests_by_physician_date,
        eligible_facilities_by_physician,
        minimum_rest_by_physician,
    )
    debug['candidates'] = 1
    debug['score_before'] = float(scoring['score'])
    debug['score_after'] = float(trial_scoring['score'])
    debug['breakdown_before'] = {
        key: float(value) for key, value in scoring['breakdown'].items()
    }
    debug['breakdown_after'] = {
        key: float(value)
        for key, value in trial_scoring['breakdown'].items()
    }
    debug['assignment_distance'] = optimizer._state_assignment_distance(
        state,
        trial,
    )
    if optimizer._optimization_priority(
        trial_scoring
    ) < optimizer._optimization_priority(scoring):
        debug['accepted'] = True
        return trial, trial_scoring, debug
    return optimizer._copy_state(state), scoring, debug
