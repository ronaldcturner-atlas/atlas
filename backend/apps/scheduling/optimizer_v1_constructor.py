"""Compatibility provider for Atlas's proven Fresh Fill placement pipeline."""

from decimal import Decimal
from datetime import timedelta

from .models import OptimizerRun


def construct_complete_fresh_fill_schedule(
    schedule_version,
    *,
    optimizer_run,
    created_by=None,
    stop_requested=None,
    progress_callback=None,
):
    """Run only the proven V1 Fresh Fill construction pipeline.

    The import remains local while the placement phase is extracted from the
    legacy optimizer module. This provider is the only compatibility edge the
    engine-neutral constructor is allowed to use.
    """
    from .optimizer import optimize_schedule_version

    return optimize_schedule_version(
        schedule_version,
        created_by=created_by,
        optimizer_run=optimizer_run,
        seed=optimizer_run.seed,
        start_mode=OptimizerRun.StartMode.FRESH_FILL,
        source_run=None,
        max_runtime_seconds=optimizer_run.max_runtime_seconds,
        optimization_focus=optimizer_run.optimization_focus,
        adaptive_runtime=True,
        stop_requested=stop_requested,
        progress_callback=progress_callback,
        isolated_run=True,
        finalize_run=False,
        construction_only=True,
    )


def fill_open_instances(context, ordered_instances, phase):
    """Apply the proven candidate ranking to one ordered placement phase.

    This is a mechanical extraction of the V1 Fresh Fill placement loop. The
    context intentionally keeps the existing scoring helpers authoritative
    while the loop is separated from the legacy repair controller.
    """
    from . import optimizer as legacy

    state = context['state']
    diagnostics = context['diagnostics']
    for instance in ordered_instances:
        if context['runtime_exceeded']():
            context['mark_timeout'](phase)
            break
        while len(state[instance.id]) < instance.required_staffing:
            if context['runtime_exceeded']():
                context['mark_timeout'](phase)
                break
            if phase == 'night':
                diagnostics['night_block_assignment_attempts'] += 1
            candidates = []
            recovery_conflict_candidates = 0
            for physician in context['shuffle'](context['physicians']):
                diagnostics['candidates_considered_before_timeout'] += 1
                if context['runtime_exceeded']():
                    context['mark_timeout'](phase)
                    break
                if physician.id in state[instance.id]:
                    continue
                if instance.facility_id not in context[
                    'eligible_facilities_by_physician'
                ].get(physician.id, set()):
                    continue
                if not legacy._can_assign_in_state(
                    state,
                    context['instances_by_id'],
                    instance,
                    physician.id,
                    context['eligible_facilities_by_physician'],
                    context['minimum_rest_by_physician'],
                    assigned_intervals=context['initial_fill_intervals'][
                        physician.id
                    ],
                ):
                    diagnostics['rest_violations_blocked'] += 1
                    continue

                contract = context['contract_by_physician'][physician.id]
                target = context['targets'][physician.id]
                shift_hours = legacy._shift_hours(instance)
                next_hours = (
                    context['initial_fill_hours'][physician.id] + shift_hours
                )
                next_shifts = context['initial_fill_shifts'][physician.id] + 1
                workload_score = legacy._workload_candidate_score(
                    target, next_hours, next_shifts,
                )
                workload_rule_delta = legacy._workload_rule_delta_from_totals(
                    context['workload_ranges_by_physician'][physician.id],
                    context['workload_totals_by_physician'][physician.id],
                    instance.date,
                    shift_hours,
                )
                workload_settings = (
                    contract.workload_settings
                    if isinstance(contract.workload_settings, dict)
                    else {}
                )
                max_days_in_row = legacy._configured_positive_int(
                    workload_settings, 'max_days_in_row',
                )
                max_days_penalty = legacy._configured_positive_penalty(
                    workload_settings,
                    'max_days_in_row_penalty_weight',
                    Decimal('0'),
                )
                assigned_dates = context['initial_fill_assigned_dates'][
                    physician.id
                ]
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
                same_shift_delta = (
                    legacy._same_shift_candidate_delta_from_indexes(
                        contract,
                        context['initial_fill_template_positions'],
                        context['initial_fill_template_indexes'],
                        physician.id,
                        instance,
                    )
                )
                if phase == 'night':
                    night_delta = legacy._night_volume_delta_from_totals(
                        context['night_ranges_by_physician'][physician.id],
                        context['night_totals_by_physician'][physician.id],
                        instance.date,
                    )
                    night_pressure = legacy._night_volume_pressure_from_totals(
                        context['night_ranges_by_physician'][physician.id],
                        context['night_totals_by_physician'][physician.id],
                        instance.date,
                    )
                else:
                    night_delta = Decimal('0')
                    night_pressure = Decimal('0')
                if legacy.NIGHT_CONSTRUCTION_HEURISTICS_ENABLED:
                    night_block_bonus = legacy._night_block_extension_bonus(
                        context['instances_by_id'],
                        state,
                        context['contract_by_physician'],
                        physician.id,
                        instance,
                    )
                    recovery_penalty = (
                        legacy._night_recovery_candidate_penalty(
                            context['instances'],
                            context['physicians'],
                            state,
                            context['contract_by_physician'],
                            physician.id,
                            instance,
                        )
                    )
                    if phase == 'night':
                        settings = legacy._night_settings(contract)
                        min_consecutive = legacy._configured_positive_int(
                            settings, 'min_consecutive_night_shifts',
                        )
                        max_consecutive = legacy._configured_positive_int(
                            settings, 'max_consecutive_night_shifts',
                        )
                        prior_dates = context['initial_fill_night_dates'][
                            physician.id
                        ]
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
                                night_block_deficit = (
                                    projected_run_length - max_consecutive
                                )
                            elif (
                                min_consecutive is not None
                                and min_consecutive > 1
                                and projected_run_length <= min_consecutive
                            ):
                                night_block_priority = 0
                                night_block_deficit = max(
                                    min_consecutive - projected_run_length, 0,
                                )
                            else:
                                night_block_priority = 1
                                night_block_deficit = 0
                        else:
                            night_block_priority = 1
                            night_block_deficit = 0
                    else:
                        night_block_priority = 1
                        night_block_deficit = 0
                    if legacy.NIGHT_BLOCK_BUILDER_ENABLED:
                        night_delta += legacy._night_candidate_delta(
                            context['instances'],
                            context['physicians'],
                            state,
                            context['contract_by_physician'],
                            physician.id,
                            instance,
                        )
                        night_minimum_bonus = (
                            legacy._night_minimum_candidate_bonus(
                                context['instances'],
                                state,
                                context['contract_by_physician'],
                                physician.id,
                                instance,
                            )
                        )
                    else:
                        night_minimum_bonus = Decimal('0')
                else:
                    night_block_bonus = Decimal('0')
                    night_minimum_bonus = Decimal('0')
                    recovery_penalty = Decimal('0')
                    night_block_priority = 1
                    night_block_deficit = 0
                if legacy._is_weekend_designated(instance):
                    weekend_settings = (
                        contract.weekend_settings
                        if isinstance(contract.weekend_settings, dict)
                        else {}
                    )
                    min_weekend_shifts = legacy._configured_positive_int(
                        weekend_settings, 'min_consecutive_weekend_shifts',
                    )
                    max_weekend_shifts = legacy._configured_positive_int(
                        weekend_settings, 'max_consecutive_weekend_shifts',
                    )
                    min_weekends = legacy._configured_positive_int(
                        weekend_settings, 'min_consecutive_weekends',
                    )
                    max_weekends = legacy._configured_positive_int(
                        weekend_settings, 'max_consecutive_weekends',
                    )
                    prior_weekend_dates = context[
                        'initial_fill_weekend_dates'
                    ][physician.id]
                    previous_weekend_date = instance.date - timedelta(days=1)
                    previous_weekend_run_length = 0
                    while previous_weekend_date in prior_weekend_dates:
                        previous_weekend_run_length += 1
                        previous_weekend_date -= timedelta(days=1)
                    projected_weekend_run_length = (
                        previous_weekend_run_length + 1
                    )
                    weekend_start = instance.date - timedelta(
                        days=instance.date.weekday()
                    )
                    prior_weekend_weeks = context[
                        'initial_fill_weekend_weeks'
                    ][physician.id]
                    opens_new_weekend = weekend_start not in prior_weekend_weeks
                    previous_week = weekend_start - timedelta(days=7)
                    previous_week_run_length = 0
                    while previous_week in prior_weekend_weeks:
                        previous_week_run_length += 1
                        previous_week -= timedelta(days=7)
                    projected_week_run_length = previous_week_run_length + 1
                    if (
                        previous_weekend_run_length
                        and max_weekend_shifts is not None
                        and projected_weekend_run_length > max_weekend_shifts
                    ) or (
                        opens_new_weekend
                        and previous_week_run_length
                        and max_weekends is not None
                        and projected_week_run_length > max_weekends
                    ):
                        weekend_block_priority = 3
                    elif (
                        previous_weekend_run_length
                        and min_weekend_shifts is not None
                        and min_weekend_shifts > 1
                        and projected_weekend_run_length <= min_weekend_shifts
                    ) or (
                        opens_new_weekend
                        and previous_week_run_length
                        and min_weekends is not None
                        and min_weekends > 1
                        and projected_week_run_length <= min_weekends
                    ):
                        weekend_block_priority = 0
                    else:
                        weekend_block_priority = 1
                else:
                    weekend_block_priority = 1
                if recovery_penalty > 0:
                    recovery_conflict_candidates += 1

                matching_requests = legacy._requests_for_shift(
                    context['requests_by_physician_date'],
                    physician.id,
                    instance,
                )
                request_score = legacy._request_candidate_rank(
                    matching_requests, contract,
                )
                workload_rank, workload_debug = (
                    legacy._initial_fill_workload_guard(
                        context['workload_ranges_by_physician'][physician.id],
                        {
                            'date': instance.date,
                            'values': context['workload_totals_by_physician'][
                                physician.id
                            ],
                        },
                        shift_hours,
                    )
                )
                workload_scarcity = Decimal('0')
                if (
                    context['start_mode'] == OptimizerRun.StartMode.FRESH_FILL
                    and workload_rank == 0
                ):
                    remaining_by_rule = {
                        index: sum(
                            context['initial_fill_open_capacity'][facility_id][(
                                row['window_start'],
                                row['window_end'],
                                row['units'],
                            )]
                            for facility_id in context[
                                'eligible_facilities_by_physician'
                            ][physician.id]
                        )
                        for index, row in enumerate(
                            context['workload_ranges_by_physician'][physician.id]
                        )
                        if row['min_value'] is not None
                        and row['window_start'] <= instance.date <= row['window_end']
                    }
                    workload_scarcity = (
                        legacy._initial_fill_workload_scarcity(
                            context['workload_ranges_by_physician'][physician.id],
                            context['workload_totals_by_physician'][physician.id],
                            remaining_by_rule,
                            instance.date,
                        )
                    )
                if workload_rank == 2:
                    diagnostics[
                        'initial_fill_workload_guard_candidates_above_max'
                    ] += 1
                    diagnostics[
                        'initial_fill_workload_guard_candidates_deprioritized'
                    ] += 1
                    examples = diagnostics['initial_fill_workload_guard_examples']
                    if len(examples) < 10:
                        examples.append({
                            'physician_id': physician.id,
                            'physician': legacy._physician_display_name(physician),
                            **legacy._contract_rule_identity(contract),
                            **workload_debug,
                        })
                candidates.append((
                    recovery_penalty > 0,
                    request_score,
                    consecutive_days_delta > 0,
                    consecutive_days_delta,
                    night_block_priority,
                    weekend_block_priority,
                    (
                        workload_rank
                        if context['start_mode']
                        == OptimizerRun.StartMode.FRESH_FILL
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
                    + (
                        recovery_penalty
                        * legacy.RECOVERY_CONFLICT_AVOIDANCE_MULTIPLIER
                    ),
                    context['rng'].random(),
                    physician,
                ))

            if context['is_timed_out']() or not candidates:
                break
            clean_candidates = [candidate for candidate in candidates if not candidate[0]]
            candidate_pool = clean_candidates or candidates
            if phase == 'non_night' and recovery_conflict_candidates:
                if clean_candidates:
                    diagnostics[
                        'nonnight_assignments_blocked_by_recovery'
                    ] += recovery_conflict_candidates
                else:
                    diagnostics[
                        'nonnight_assignments_allowed_despite_recovery'
                    ] += 1
            selected_physician = min(candidate_pool)[-1]
            state[instance.id].append(selected_physician.id)
            if context['initial_fill_opportunity_windows']:
                for window_start, window_end, units in context[
                    'initial_fill_opportunity_windows'
                ]:
                    if window_start <= instance.date <= window_end:
                        key = (window_start, window_end, units)
                        slot_units = (
                            Decimal('1')
                            if units == 'SHIFTS'
                            else legacy._shift_hours(instance)
                        )
                        context['initial_fill_open_capacity'][
                            instance.facility_id
                        ][key] -= slot_units
            context['initial_fill_intervals'][selected_physician.id].append((
                instance.start_datetime, instance.end_datetime,
            ))
            context['initial_fill_hours'][selected_physician.id] += (
                legacy._shift_hours(instance)
            )
            context['initial_fill_shifts'][selected_physician.id] += 1
            context['initial_fill_assigned_dates'][selected_physician.id].add(
                instance.date
            )
            selected_position = context['initial_fill_template_positions'].get(
                instance.id
            )
            if selected_position is not None:
                template_id, occurrence_index = selected_position
                context['initial_fill_template_indexes'][
                    (selected_physician.id, template_id)
                ].append(occurrence_index)
            for row in context['workload_ranges_by_physician'][
                selected_physician.id
            ]:
                if row['window_start'] <= instance.date <= row['window_end']:
                    key = (row['window_start'], row['window_end'], row['units'])
                    context['workload_totals_by_physician'][
                        selected_physician.id
                    ][key] += (
                        Decimal('1')
                        if row['units'] == 'SHIFTS'
                        else legacy._shift_hours(instance)
                    )
            if phase == 'night':
                context['initial_fill_night_dates'][selected_physician.id].add(
                    instance.date
                )
                for row in context['night_ranges_by_physician'][
                    selected_physician.id
                ]:
                    if row['window_start'] <= instance.date <= row['window_end']:
                        key = (row['window_start'], row['window_end'])
                        context['night_totals_by_physician'][
                            selected_physician.id
                        ][key] += Decimal('1')
            if legacy._is_weekend_designated(instance):
                context['initial_fill_weekend_dates'][selected_physician.id].add(
                    instance.date
                )
                context['initial_fill_weekend_weeks'][selected_physician.id].add(
                    instance.date - timedelta(days=instance.date.weekday())
                )
            diagnostics['assignments_made'] += 1
            if phase == 'night':
                diagnostics['night_block_assignment_successes'] += 1


def build_night_blocks(context):
    """Apply the proven V1 night-block construction phase."""
    from . import optimizer as legacy

    state = context['state']
    diagnostics = context['diagnostics']
    before_scoring = legacy._score_schedule(
        context['instances'],
        context['physicians'],
        state,
        context['targets'],
        context['contract_by_physician'],
        context['requests_by_physician_date'],
        context['eligible_facilities_by_physician'],
        context['minimum_rest_by_physician'],
        include_internal_night_heuristics=True,
    )
    diagnostics['night_block_builder_score_before'] = float(
        before_scoring['score']
    )
    before_status = legacy._night_minimum_status(
        context['instances'],
        context['physicians'],
        state,
        context['contract_by_physician'],
    )
    diagnostics['physicians_below_night_min_before_night_build'] = (
        before_status['physicians_under_night_minimum']
    )

    while True:
        if context['runtime_exceeded']():
            context['mark_timeout']('night_block_builder')
            break
        unfilled_nights = [
            instance for instance in context['night_instances']
            if len(state[instance.id]) < instance.required_staffing
        ]
        if not unfilled_nights:
            break

        minimum_status = legacy._night_minimum_status(
            context['instances'],
            context['physicians'],
            state,
            context['contract_by_physician'],
        )
        under_minimum_ids = {
            row['physician_id']
            for row in minimum_status['physicians_under_night_minimum']
        }
        current_under_deficit = context['night_rule_window_deficit'](
            minimum_status['physicians_under_night_minimum']
        )
        candidates = []
        windows = context['shuffle'](
            context['consecutive_night_windows'](unfilled_nights)
        )
        for physician in context['shuffle'](context['physicians']):
            if context['runtime_exceeded']():
                context['mark_timeout']('night_block_builder')
                break
            physician_windows = context['shuffle'](windows)
            for window in physician_windows:
                if context['runtime_exceeded']():
                    context['mark_timeout']('night_block_builder')
                    break
                for length in context['block_candidate_lengths'](
                    physician.id, window,
                ):
                    if context['runtime_exceeded']():
                        context['mark_timeout']('night_block_builder')
                        break
                    block = window[:length]
                    if not block:
                        continue
                    diagnostics['night_block_assignment_attempts'] += 1
                    trial_state = legacy._copy_state(state)
                    rejected = None
                    for instance in block:
                        if (
                            len(trial_state[instance.id])
                            >= instance.required_staffing
                        ):
                            rejected = 'filled'
                            break
                        if physician.id in trial_state[instance.id]:
                            rejected = 'duplicate'
                            break
                        if instance.facility_id not in context[
                            'eligible_facilities_by_physician'
                        ].get(physician.id, set()):
                            rejected = 'facility_ineligible'
                            break
                        if not legacy._can_assign_in_state(
                            trial_state,
                            context['instances_by_id'],
                            instance,
                            physician.id,
                            context['eligible_facilities_by_physician'],
                            context['minimum_rest_by_physician'],
                        ):
                            rejected = 'rest_or_overlap'
                            diagnostics['rest_violations_blocked'] += 1
                            break
                        legacy._add_to_state(
                            trial_state, instance.id, physician.id,
                        )
                    if rejected is not None:
                        diagnostics[
                            'night_block_builder_rejections_by_reason'
                        ][rejected] += 1
                        continue

                    diagnostics['night_block_builder_candidates_created'] += 1
                    trial_scoring = legacy._score_schedule(
                        context['instances'],
                        context['physicians'],
                        trial_state,
                        context['targets'],
                        context['contract_by_physician'],
                        context['requests_by_physician_date'],
                        context['eligible_facilities_by_physician'],
                        context['minimum_rest_by_physician'],
                        include_internal_night_heuristics=True,
                    )
                    trial_status = legacy._night_minimum_status(
                        context['instances'],
                        context['physicians'],
                        trial_state,
                        context['contract_by_physician'],
                    )
                    trial_report = legacy._night_violation_report(
                        context['instances'],
                        context['physicians'],
                        trial_state,
                        context['contract_by_physician'],
                    )
                    trial_under_deficit = context[
                        'night_rule_window_deficit'
                    ](trial_status['physicians_under_night_minimum'])
                    candidates.append((
                        0 if physician.id in under_minimum_ids else 1,
                        trial_under_deficit,
                        context['night_recovery_conflict_count'](
                            trial_report
                        ),
                        -len(block),
                        trial_scoring['score'],
                        context['rng'].random(),
                        physician,
                        block,
                        trial_state,
                    ))

        if not candidates:
            break

        under_candidates = [
            candidate for candidate in candidates if candidate[0] == 0
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
        diagnostics['assignments_made'] += len(selected_block)
        diagnostics['night_block_assignment_successes'] += len(
            selected_block
        )
        diagnostics['night_block_builder_lengths_assigned'].append(
            len(selected_block)
        )
        diagnostics['night_block_builder_assigned_blocks'].append({
            'physician_id': selected_physician.id,
            'physician': legacy._physician_display_name(
                selected_physician
            ),
            **legacy._contract_rule_identity(
                context['contract_by_physician'].get(selected_physician.id)
            ),
            'length': len(selected_block),
            'dates': legacy._block_dates(selected_block),
            'shift_instance_ids': [
                instance.id for instance in selected_block
            ],
            'facilities': sorted({
                instance.facility.short_name or instance.facility.name
                for instance in selected_block
            }),
        })

    after_scoring = legacy._score_schedule(
        context['instances'],
        context['physicians'],
        state,
        context['targets'],
        context['contract_by_physician'],
        context['requests_by_physician_date'],
        context['eligible_facilities_by_physician'],
        context['minimum_rest_by_physician'],
        include_internal_night_heuristics=True,
    )
    diagnostics['night_block_builder_score_after'] = float(
        after_scoring['score']
    )
    after_status = legacy._night_minimum_status(
        context['instances'],
        context['physicians'],
        state,
        context['contract_by_physician'],
    )
    diagnostics['physicians_below_night_min_after_night_build'] = (
        after_status['physicians_under_night_minimum']
    )
    after_report = legacy._night_violation_report(
        context['instances'],
        context['physicians'],
        state,
        context['contract_by_physician'],
    )
    diagnostics['night_recovery_conflicts_after_night_build'] = context[
        'night_recovery_conflict_count'
    ](after_report)
    diagnostics['night_distribution_by_physician_after_build'] = context[
        'night_distribution_rows'
    ](after_report)
