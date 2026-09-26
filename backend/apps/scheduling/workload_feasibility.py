from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .models import ContractUserAssignment, ScheduleRequest, ScheduleShiftAssignment
from .optimizer import (
    _version_contract_target,
    _decimal_or_none,
    _effective_workload_rule,
    _is_weekend_designated,
    _configured_positive_int,
    _configured_positive_penalty,
    _minimum_rest_hours,
    _overlaps,
    _period_windows,
    _physician_display_name,
    _requests_for_shift,
    _rest_violation,
    _shift_hours,
    _unique_night_period_rules,
)
from .run_state import assignments_for_viewed_run


def _number(value):
    return float(value) if value is not None else None


def _manual_only_fixed_coverage_by_instance(
    instances, visible_assignments, manual_only_physician_ids, schedule_requests,
):
    """Return capped fixed coverage supplied by manual-only physicians.

    A Shift On request is an authoritative fixed assignment for a physician
    whose contract is manual-assignment-only. Count that instruction before an
    optimizer run has materialized its locked assignment row, while deduping it
    against any row that already exists.
    """
    instance_by_id = {instance.id: instance for instance in instances}
    instances_by_date_template = defaultdict(list)
    for instance in instances:
        instances_by_date_template[(instance.date, instance.shift_template_id)].append(
            instance
        )
    for matching_instances in instances_by_date_template.values():
        matching_instances.sort(
            key=lambda item: (item.start_datetime, item.end_datetime, item.id)
        )

    physicians_by_instance = defaultdict(set)
    for assignment in visible_assignments:
        if (
            assignment.physician_id in manual_only_physician_ids
            and assignment.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
            and assignment.shift_instance_id in instance_by_id
        ):
            physicians_by_instance[assignment.shift_instance_id].add(
                assignment.physician_id
            )

    for schedule_request in schedule_requests:
        if (
            schedule_request.physician_id not in manual_only_physician_ids
            or schedule_request.request_type != ScheduleRequest.RequestType.SHIFT_ON
        ):
            continue
        matching_instances = []
        for template in schedule_request.shift_templates.all():
            matching_instances.extend(
                instances_by_date_template.get(
                    (schedule_request.date, template.id), ()
                )
            )
        matching_instances.sort(
            key=lambda item: (item.start_datetime, item.end_datetime, item.id)
        )
        if matching_instances:
            physicians_by_instance[matching_instances[0].id].add(
                schedule_request.physician_id
            )
    return {
        instance_id: min(
            len(physician_ids), instance_by_id[instance_id].required_staffing,
        )
        for instance_id, physician_ids in physicians_by_instance.items()
    }


def _request_off_feasibility(
    instances, contract_assignments, schedule_requests,
    manual_only_fixed_coverage=None,
):
    """Find dates that cannot cover concurrent slots if requests off are honored."""
    manual_only_fixed_coverage = manual_only_fixed_coverage or {}
    instances_by_date = defaultdict(list)
    for instance in instances:
        instances_by_date[instance.date].append(instance)

    physicians = {}
    eligible_facilities = {}
    for assignment in contract_assignments:
        if assignment.contract.manual_assignment_only:
            continue
        physician = assignment.physician
        physicians[physician.id] = physician
        eligible_facilities[physician.id] = {
            facility.id for facility in assignment.contract.facilities.all()
        }

    requests_by_physician_date = defaultdict(list)
    for schedule_request in schedule_requests:
        requests_by_physician_date[
            (schedule_request.physician_id, schedule_request.date)
        ].append(schedule_request)

    def physician_can_cover(physician_id, instance):
        if instance.facility_id not in eligible_facilities.get(physician_id, set()):
            return False
        return not any(
            request.request_type in {
                ScheduleRequest.RequestType.DAY_OFF,
                ScheduleRequest.RequestType.SHIFT_OFF,
            }
            for request in _requests_for_shift(
                requests_by_physician_date, physician_id, instance,
            )
        )

    def maximum_slot_matching(active_instances):
        slots = []
        for instance in active_instances:
            remaining_staffing = max(
                instance.required_staffing
                - manual_only_fixed_coverage.get(instance.id, 0),
                0,
            )
            slots.extend([instance] * remaining_staffing)
        candidates_by_slot = [
            [
                physician_id for physician_id in physicians
                if physician_can_cover(physician_id, instance)
            ]
            for instance in slots
        ]
        slot_order = sorted(range(len(slots)), key=lambda index: len(candidates_by_slot[index]))
        physician_to_slot = {}

        def assign_slot(slot_index, visited_physicians):
            for physician_id in candidates_by_slot[slot_index]:
                if physician_id in visited_physicians:
                    continue
                visited_physicians.add(physician_id)
                prior_slot = physician_to_slot.get(physician_id)
                if prior_slot is None or assign_slot(prior_slot, visited_physicians):
                    physician_to_slot[physician_id] = slot_index
                    return True
            return False

        matched = 0
        for slot_index in slot_order:
            if assign_slot(slot_index, set()):
                matched += 1
        return len(slots), matched

    affected_dates = []
    for target_date, date_instances in sorted(instances_by_date.items()):
        boundaries = sorted({
            boundary
            for instance in date_instances
            for boundary in (instance.start_datetime, instance.end_datetime)
        })
        worst_window = None
        for window_start, window_end in zip(boundaries, boundaries[1:]):
            active_instances = [
                instance for instance in date_instances
                if instance.start_datetime < window_end and instance.end_datetime > window_start
            ]
            if not active_instances:
                continue
            required, maximum_staffable = maximum_slot_matching(active_instances)
            shortage = required - maximum_staffable
            if shortage <= 0:
                continue
            if worst_window is None or shortage > worst_window['shortage']:
                requested_off_ids = {
                    physician_id for physician_id in physicians
                    if any(
                        request.request_type in {
                            ScheduleRequest.RequestType.DAY_OFF,
                            ScheduleRequest.RequestType.SHIFT_OFF,
                        }
                        for instance in active_instances
                        for request in _requests_for_shift(
                            requests_by_physician_date, physician_id, instance,
                        )
                    )
                }
                worst_window = {
                    'date': target_date.isoformat(),
                    'window_start': window_start.isoformat(),
                    'window_end': window_end.isoformat(),
                    'required_staffing': required,
                    'maximum_staffable': maximum_staffable,
                    'shortage': shortage,
                    'physicians_requested_off': len(requested_off_ids),
                }
        if worst_window:
            affected_dates.append(worst_window)

    feasible = not affected_dates
    return {
        'status': 'feasible' if feasible else 'infeasible',
        'dates_checked': len(instances_by_date),
        'affected_dates': affected_dates,
        'interpretation': (
            'Every date has enough request-free, facility-eligible physicians for its concurrent staffing needs.'
            if feasible else
            f'{len(affected_dates)} date(s) cannot be fully staffed if all requests off are honored.'
        ),
        'scope_note': (
            'Honors all day-off and shift-off requests regardless of request weight and accounts for facility '
            'eligibility, overlapping shifts, and exact fixed coverage from manual-only physicians. '
            'Manual-only physicians are not treated as available for other shifts. It does not include rest '
            'rules, workload limits, or other cross-date constraints.'
        ),
    }


def _aggregate_hour_rule_bounds(rule_rows):
    """Sum disjoint windows, then intersect simultaneous period-rule totals.

    Monthly and block rules constrain the same hours, not extra capacity.
    Duplicate rules for a window also intersect rather than add.
    """
    periods = defaultdict(lambda: defaultdict(list))
    for row in rule_rows:
        periods[row['period']][(row['period_start'], row['period_end'])].append(row)
    minimum_totals = []
    maximum_totals = []
    for windows in periods.values():
        minimum_total = Decimal('0')
        maximum_total = Decimal('0')
        bounded = True
        for rows in windows.values():
            minimum_total += max(
                Decimal(str(row['effective_min_hours'] or 0)) for row in rows
            )
            maxima = [
                Decimal(str(row['effective_max_hours'])) for row in rows
                if row['effective_max_hours'] is not None
            ]
            if maxima:
                maximum_total += min(maxima)
            else:
                bounded = False
        minimum_totals.append(minimum_total)
        if bounded:
            maximum_totals.append(maximum_total)
    return (
        max(minimum_totals, default=Decimal('0')),
        min(maximum_totals) if maximum_totals else None,
    )


def _fte_adjustment_preview(status, physician_rows, available_hours, aggregate_min, total_max):
    if status == 'maximum_infeasible':
        candidates = [
            row for row in physician_rows
            if row['effective_max_hours'] is not None and Decimal(str(row['fte'])) > 0
        ]
        required = available_hours - total_max
        direction = 'increase_maximum'
        current_key = 'effective_max_hours'
    elif status == 'minimum_infeasible':
        candidates = [
            row for row in physician_rows
            if row['effective_min_hours'] is not None
            and row['effective_min_hours'] > 0
            and Decimal(str(row['fte'])) > 0
        ]
        required = aggregate_min - available_hours
        direction = 'decrease_minimum'
        current_key = 'effective_min_hours'
    else:
        return None

    total_fte = sum((Decimal(str(row['fte'])) for row in candidates), Decimal('0'))
    if not candidates or total_fte <= 0:
        return {
            'direction': direction,
            'required_adjustment_hours': _number(required),
            'total_applicable_fte': _number(total_fte),
            'adjustment_hours_per_fte': None,
            'proposals': [],
            'can_preview': False,
            'reason': 'No applicable physicians have a positive FTE and bounded hour range.',
        }

    base_rate = required / total_fte
    adjustments = {row['physician_id']: Decimal('0') for row in candidates}
    if direction == 'increase_maximum':
        for row in candidates:
            adjustments[row['physician_id']] = base_rate * Decimal(str(row['fte']))
    else:
        remaining = required
        active = list(candidates)
        while remaining > Decimal('0.000001') and active:
            active_fte = sum((Decimal(str(row['fte'])) for row in active), Decimal('0'))
            rate = remaining / active_fte
            applied = Decimal('0')
            next_active = []
            for row in active:
                physician_id = row['physician_id']
                current = Decimal(str(row[current_key])) - adjustments[physician_id]
                proposed = rate * Decimal(str(row['fte']))
                reduction = min(current, proposed)
                adjustments[physician_id] += reduction
                applied += reduction
                if current - reduction > Decimal('0.000001'):
                    next_active.append(row)
            if applied <= Decimal('0.000001'):
                break
            remaining -= applied
            active = next_active

    proposals = []
    for row in candidates:
        current = Decimal(str(row[current_key]))
        adjustment = adjustments[row['physician_id']]
        proposed = (
            current + adjustment
            if direction == 'increase_maximum'
            else current - adjustment
        )
        proposals.append({
            'physician_id': row['physician_id'],
            'physician': row['physician'],
            'fte': row['fte'],
            'current_hours': _number(current),
            'adjustment_hours': _number(adjustment),
            'proposed_hours': _number(max(proposed, Decimal('0'))),
        })
    applied_total = sum(adjustments.values(), Decimal('0'))
    return {
        'direction': direction,
        'required_adjustment_hours': _number(required),
        'total_applicable_fte': _number(total_fte),
        'adjustment_hours_per_fte': _number(base_rate),
        'proposals': proposals,
        'can_preview': abs(applied_total - required) <= Decimal('0.0001'),
        'reason': None,
    }


def _assignment_hours(assignments):
    return sum(
        (_shift_hours(assignment.shift_instance) for assignment in assignments),
        Decimal('0'),
    )


def _assignment_accounting(version, instances, optimizer_run):
    if optimizer_run is None:
        return None, []

    block = version.schedule_block
    run_assignments = list(
        ScheduleShiftAssignment.objects.filter(
            optimizer_run=optimizer_run,
            shift_instance__schedule_version=version,
            shift_instance__date__gte=block.start_date,
            shift_instance__date__lte=block.end_date,
        ).select_related('shift_instance__shift_template', 'physician__user')
    )
    visible_assignments = list(
        assignments_for_viewed_run(version, optimizer_run)
        .select_related('shift_instance__shift_template', 'physician__user')
    )
    visible_counts = defaultdict(int)
    visible_pair_counts = defaultdict(int)
    run_pair_counts = defaultdict(int)
    for assignment in run_assignments:
        run_pair_counts[(assignment.shift_instance_id, assignment.physician_id)] += 1
    for assignment in visible_assignments:
        visible_counts[assignment.shift_instance_id] += 1
        visible_pair_counts[(assignment.shift_instance_id, assignment.physician_id)] += 1

    def assignment_detail(assignment):
        instance = assignment.shift_instance
        return {
            'assignment_id': assignment.id,
            'shift_instance_id': instance.id,
            'shift_name': instance.shift_template.generated_name(),
            'date': instance.date.isoformat(),
            'start_datetime': instance.start_datetime.isoformat(),
            'end_datetime': instance.end_datetime.isoformat(),
            'physician_id': assignment.physician_id,
            'physician': _physician_display_name(assignment.physician),
            'assignment_source': assignment.assignment_source,
            'optimizer_run_id': assignment.optimizer_run_id,
            'is_locked': assignment.is_locked,
        }

    duplicate_assignment_list = []
    for pair, count in visible_pair_counts.items():
        if count <= 1:
            continue
        pair_rows = [
            assignment_detail(assignment)
            for assignment in visible_assignments
            if (assignment.shift_instance_id, assignment.physician_id) == pair
        ]
        duplicate_assignment_list.append({
            'shift_instance_id': pair[0],
            'physician_id': pair[1],
            'duplicate_row_count': count - 1,
            'assignments': pair_rows,
        })
    selected_run_duplicate_assignment_list = [
        {
            'shift_instance_id': pair[0],
            'physician_id': pair[1],
            'duplicate_row_count': count - 1,
            'assignments': [
                assignment_detail(assignment)
                for assignment in run_assignments
                if (assignment.shift_instance_id, assignment.physician_id) == pair
            ],
        }
        for pair, count in run_pair_counts.items()
        if count > 1
    ]

    unfilled_hours = Decimal('0')
    excess_hours = Decimal('0')
    excess_count = 0
    overstaffed_instances = 0
    for instance in instances:
        assigned_count = visible_counts[instance.id]
        missing = max(instance.required_staffing - assigned_count, 0)
        excess = max(assigned_count - instance.required_staffing, 0)
        unfilled_hours += _shift_hours(instance) * missing
        excess_hours += _shift_hours(instance) * excess
        excess_count += excess
        overstaffed_instances += int(excess > 0)

    outside_run = [
        assignment for assignment in visible_assignments
        if assignment.optimizer_run_id != optimizer_run.id
    ]
    valid_multi_staff = [
        {
            'shift_instance_id': instance.id,
            'shift_name': instance.shift_template.generated_name(),
            'date': instance.date.isoformat(),
            'required_staffing': instance.required_staffing,
            'visible_assignment_count': visible_counts[instance.id],
        }
        for instance in instances
        if instance.required_staffing > 1
        and 1 < visible_counts[instance.id] <= instance.required_staffing
    ]
    overstaffed_shifts = [
        {
            'shift_instance_id': instance.id,
            'shift_name': instance.shift_template.generated_name(),
            'date': instance.date.isoformat(),
            'required_staffing': instance.required_staffing,
            'visible_assignment_count': visible_counts[instance.id],
            'assignments': [
                assignment_detail(assignment)
                for assignment in visible_assignments
                if assignment.shift_instance_id == instance.id
            ],
        }
        for instance in instances
        if visible_counts[instance.id] > instance.required_staffing
    ]
    manual_locked = [
        assignment for assignment in visible_assignments
        if assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
        and assignment.is_locked
    ]
    accounting = {
        'total_run_assigned_hours': _number(_assignment_hours(run_assignments)),
        'total_visible_assigned_hours': _number(_assignment_hours(visible_assignments)),
        'total_unfilled_hours': _number(unfilled_hours),
        'assignment_count': len(visible_assignments),
        'run_assignment_count': len(run_assignments),
        'visible_assignment_count': len(visible_assignments),
        'duplicate_shift_assignment_count': sum(
            count - 1 for count in visible_pair_counts.values() if count > 1
        ),
        'duplicate_assignment_list': duplicate_assignment_list,
        'selected_run_duplicate_assignment_count': sum(
            count - 1 for count in run_pair_counts.values() if count > 1
        ),
        'selected_run_duplicate_assignment_list': selected_run_duplicate_assignment_list,
        'assignments_above_required_staffing_count': excess_count,
        'assignments_above_required_staffing_hours': _number(excess_hours),
        'overstaffed_shift_instance_count': overstaffed_instances,
        'visible_assignments_outside_selected_run_count': len(outside_run),
        'legacy_unscoped_manual_assignment_count': sum(
            assignment.optimizer_run_id is None
            and assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            for assignment in visible_assignments
        ),
        'inactive_physician_assignment_count': sum(
            not assignment.physician.active for assignment in visible_assignments
        ),
        'manual_assignment_count': sum(
            assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.MANUAL
            for assignment in visible_assignments
        ),
        'optimizer_assignment_count': sum(
            assignment.assignment_source == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
            for assignment in visible_assignments
        ),
        'assignments_outside_selected_schedule_version_count': 0,
        'open_or_unfilled_placeholder_assignment_count': 0,
        'non_physician_assignment_count': 0,
        'extra_assignment_categories': {
            'duplicate_same_physician_shift': duplicate_assignment_list,
            'valid_multi_staff_shift_assignments': valid_multi_staff,
            'assignments_outside_selected_run': [
                assignment_detail(assignment) for assignment in outside_run
            ],
            'assignments_outside_selected_version': [],
            'visible_only_assignments': [
                assignment_detail(assignment) for assignment in outside_run
            ],
            'manual_locked_assignments_included': [
                assignment_detail(assignment) for assignment in manual_locked
            ],
            'overstaffed_shift_instances': overstaffed_shifts,
        },
        'accounting_notes': [
            'Generated required hours equal shift duration multiplied by required staffing.',
            'Run-assigned hours count rows owned by the selected optimizer run.',
            'Visible assigned hours use canonical run visibility and may additionally include legacy unscoped manual rows for ordinary OPTIMIZER runs.',
            'Each assignment row counts the full shift-instance duration; split assignment hours are not modeled.',
            'Open and unfilled shifts are capacity gaps, not assignment rows.',
        ],
    }
    return accounting, run_assignments


def _night_feasibility(
    version, instances, contract_assignments, optimizer_run,
    manual_only_fixed_coverage=None,
):
    manual_only_fixed_coverage = manual_only_fixed_coverage or {}
    night_instances = [instance for instance in instances if instance.shift_template.night_shift]
    manual_only_physician_ids = {
        assignment.physician_id
        for assignment in contract_assignments
        if assignment.contract.manual_assignment_only
    }
    optimizer_contract_assignments = [
        assignment for assignment in contract_assignments
        if not assignment.contract.manual_assignment_only
    ]
    fixed_assignments = list(
        assignments_for_viewed_run(version, optimizer_run)
        .filter(
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            is_locked=True,
            shift_instance__shift_template__night_shift=True,
        )
        .select_related('shift_instance')
    )
    fixed_physicians_by_instance = defaultdict(set)
    night_instance_by_id = {instance.id: instance for instance in night_instances}
    for assignment in fixed_assignments:
        if (
            assignment.physician_id not in manual_only_physician_ids
            and assignment.shift_instance_id in night_instance_by_id
        ):
            fixed_physicians_by_instance[assignment.shift_instance_id].add(
                assignment.physician_id
            )

    def fixed_coverage(window_instances):
        return sum(
            min(
                len(fixed_physicians_by_instance.get(instance.id, set()))
                + manual_only_fixed_coverage.get(instance.id, 0),
                instance.required_staffing,
            )
            for instance in window_instances
        )
    period_types = {
        rule.get('period_type') or 'SCHEDULE_BLOCK'
        for assignment in optimizer_contract_assignments
        for rule in _unique_night_period_rules(
            assignment.contract.night_settings
            if isinstance(assignment.contract.night_settings, dict) else {}
        )
    } or {'SCHEDULE_BLOCK'}
    all_windows = {
        period_type: _period_windows(night_instances or instances, period_type)
        for period_type in period_types
    }

    def covers_window(windows, target_start, target_end):
        ordered = sorted(windows)
        if not ordered or ordered[0][0] != target_start or ordered[-1][1] != target_end:
            return False
        return all(
            ordered[index][1] + timedelta(days=1) == ordered[index + 1][0]
            for index in range(len(ordered) - 1)
        )

    physician_constraints = {}
    for contract_assignment in optimizer_contract_assignments:
        rules = _unique_night_period_rules(
            contract_assignment.contract.night_settings
            if isinstance(contract_assignment.contract.night_settings, dict) else {}
        )
        constraints = []
        for rule in rules:
            period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
            for window_start, window_end in all_windows[period_type]:
                fixed_count = sum(
                    assignment.physician_id == contract_assignment.physician_id
                    and window_start <= assignment.shift_instance.date <= window_end
                    for assignment in fixed_assignments
                )
                minimum = _decimal_or_none(rule.get('min_shifts')) or Decimal('0')
                maximum = _decimal_or_none(rule.get('max_shifts'))
                constraints.append({
                    'period_type': period_type,
                    'start': window_start,
                    'end': window_end,
                    'minimum': max(minimum - fixed_count, Decimal('0')),
                    'maximum': None if maximum is None else max(maximum - fixed_count, Decimal('0')),
                    'fixed_count': fixed_count,
                    'fixed_over_maximum': maximum is not None and fixed_count > maximum,
                })
        physician_constraints[contract_assignment.physician_id] = constraints

    def effective_bounds(constraints, target_start, target_end):
        minimum_candidates = []
        maximum_candidates = []
        exact = [
            row for row in constraints
            if row['start'] == target_start and row['end'] == target_end
        ]
        if exact:
            minimum_candidates.append(max(row['minimum'] for row in exact))
            exact_maxima = [row['maximum'] for row in exact if row['maximum'] is not None]
            if exact_maxima:
                maximum_candidates.append(min(exact_maxima))
        for child_type in period_types:
            children = [
                row for row in constraints
                if row['period_type'] == child_type
                and target_start <= row['start']
                and row['end'] <= target_end
                and not (row['start'] == target_start and row['end'] == target_end)
            ]
            child_windows = {(row['start'], row['end']) for row in children}
            if not covers_window(child_windows, target_start, target_end):
                continue
            by_window = defaultdict(list)
            for row in children:
                by_window[(row['start'], row['end'])].append(row)
            minimum_candidates.append(sum(
                max(row['minimum'] for row in window_rows)
                for window_rows in by_window.values()
            ))
            if all(any(row['maximum'] is not None for row in window_rows) for window_rows in by_window.values()):
                maximum_candidates.append(sum(
                    min(row['maximum'] for row in window_rows if row['maximum'] is not None)
                    for window_rows in by_window.values()
                ))
        minimum = max(minimum_candidates, default=Decimal('0'))
        maximum = min(maximum_candidates) if maximum_candidates else None
        return minimum, maximum

    rows = []
    for period_type in sorted(period_types):
        for window_start, window_end in all_windows[period_type]:
            window_instances = [
                instance for instance in night_instances
                if window_start <= instance.date <= window_end
            ]
            required = sum(instance.required_staffing for instance in window_instances)
            window_fixed_coverage = fixed_coverage(window_instances)
            remaining = max(required - window_fixed_coverage, 0)
            total_minimum = Decimal('0')
            total_maximum = Decimal('0')
            maximum_unbounded = False
            fixed_limit_violations = []
            overlapping_rule_conflicts = []
            for contract_assignment in optimizer_contract_assignments:
                constraints = physician_constraints[contract_assignment.physician_id]
                if not constraints:
                    maximum_unbounded = True
                    continue
                minimum, maximum = effective_bounds(constraints, window_start, window_end)
                total_minimum += minimum
                if maximum is None:
                    maximum_unbounded = True
                else:
                    total_maximum += maximum
                    if minimum > maximum:
                        overlapping_rule_conflicts.append({
                            'physician': _physician_display_name(contract_assignment.physician),
                            'effective_minimum': _number(minimum),
                            'effective_maximum': _number(maximum),
                        })
                for constraint in constraints:
                    if constraint['fixed_over_maximum']:
                        fixed_limit_violations.append({
                            'physician': _physician_display_name(contract_assignment.physician),
                            'fixed_nights': constraint['fixed_count'],
                            'configured_maximum': _number(constraint['fixed_count'] + (constraint['maximum'] or 0)),
                        })
            bounded_maximum = None if maximum_unbounded else total_maximum
            if overlapping_rule_conflicts:
                status = 'penalty_unavoidable'
                interpretation = 'Overlapping night rules make at least one volume penalty unavoidable.'
            elif fixed_limit_violations:
                status = 'penalty_unavoidable'
                interpretation = 'A fixed manual night assignment already exceeds a preferred maximum.'
            elif Decimal(remaining) < total_minimum:
                status = 'penalty_unavoidable'
                interpretation = 'There are not enough remaining nights to satisfy every preferred minimum.'
            elif bounded_maximum is not None and Decimal(remaining) > bounded_maximum:
                status = 'penalty_unavoidable'
                interpretation = 'Some remaining nights must exceed a preferred physician maximum.'
            else:
                status = 'feasible'
                interpretation = 'Remaining night shifts fit within remaining aggregate night limits.'
            rows.append({
                'period_type': period_type,
                'period_start': window_start.isoformat(),
                'period_end': window_end.isoformat(),
                'required_night_shifts': required,
                'fixed_manual_night_shifts': window_fixed_coverage,
                'remaining_night_shifts': remaining,
                'remaining_minimum_night_shifts': _number(total_minimum),
                'remaining_maximum_night_shifts': _number(bounded_maximum),
                'status': status,
                'interpretation': interpretation,
                'fixed_limit_violations': fixed_limit_violations,
                'overlapping_rule_conflicts': overlapping_rule_conflicts,
            })
    return {
        'status': 'penalty_unavoidable' if any(row['status'] != 'feasible' for row in rows) else 'feasible',
        'fixed_manual_night_shifts': fixed_coverage(night_instances),
        'periods': rows,
        'scope_note': (
            'Locked manual night assignments and Shift On requests for manual-only physicians are treated as fixed coverage. '
            'Manual-only physicians contribute no additional night capacity. Eligibility, rest, and exact '
            'night-block patterns are not included.'
        ),
    }


def _weekend_feasibility(
    instances, contract_assignments, visible_assignments, schedule_requests,
):
    """Report necessary, contract-defined weekend conditions, not a solver verdict.

    Only locked assignments and manual-only Shift On instructions are treated
    as inevitable. Candidate capacity is intentionally optimistic: ignoring
    rest and competing rules cannot manufacture a false impossibility.
    """
    weekend_instances = [item for item in instances if _is_weekend_designated(item)]
    instance_by_id = {item.id: item for item in instances}
    by_date_template = defaultdict(list)
    for item in instances:
        by_date_template[(item.date, item.shift_template_id)].append(item)
    for matches in by_date_template.values():
        matches.sort(key=lambda item: (item.start_datetime, item.id))

    manual_only_ids = {
        row.physician_id for row in contract_assignments
        if row.contract.manual_assignment_only
    }
    fixed_by_instance = defaultdict(set)
    for row in visible_assignments:
        if row.is_locked and row.shift_instance_id in instance_by_id:
            fixed_by_instance[row.shift_instance_id].add(row.physician_id)
    for request in schedule_requests:
        if (request.physician_id not in manual_only_ids
                or request.request_type != ScheduleRequest.RequestType.SHIFT_ON):
            continue
        matches = sorted(
            (item for template in request.shift_templates.all()
             for item in by_date_template.get((request.date, template.id), ())),
            key=lambda item: (item.start_datetime, item.id),
        )
        if matches:
            fixed_by_instance[matches[0].id].add(request.physician_id)

    optimizer_rows = [
        row for row in contract_assignments if row.physician_id not in manual_only_ids
    ]
    fixed_weekend = {
        item.id: fixed_by_instance.get(item.id, set())
        for item in weekend_instances
    }
    conflicts = []
    checked_rules = 0
    period_bounds = defaultdict(dict)
    for row in optimizer_rows:
        physician_id = row.physician_id
        physician = _physician_display_name(row.physician)
        settings = row.contract.weekend_settings if isinstance(row.contract.weekend_settings, dict) else {}
        eligible_facilities = {facility.id for facility in row.contract.facilities.all()}
        eligible = [
            item for item in weekend_instances
            if item.facility_id in eligible_facilities
        ]
        fixed = [
            item for item in weekend_instances
            if physician_id in fixed_weekend[item.id]
        ]
        for rule in settings.get('period_rules') or []:
            if not isinstance(rule, dict):
                continue
            period = rule.get('period_type') or 'SCHEDULE_BLOCK'
            for start, end in _period_windows(instances, period):
                key = (period, start, end)
                fixed_count = sum(start <= item.date <= end for item in fixed)
                possible_extra = sum(
                    max(item.required_staffing - len(fixed_weekend[item.id]), 0)
                    for item in eligible if start <= item.date <= end
                    and physician_id not in fixed_weekend[item.id]
                )
                bounds = period_bounds[key].setdefault(physician_id, {'minimum': Decimal('0'), 'maximum': None, 'fixed': fixed_count})
                for side in ('min', 'max'):
                    limit = _decimal_or_none(rule.get(f'{side}_volume'))
                    weight = _configured_positive_penalty(rule, f'{side}_penalty_weight', Decimal('0'))
                    if limit is None or weight <= 0:
                        continue
                    checked_rules += 1
                    if side == 'min':
                        bounds['minimum'] = max(bounds['minimum'], limit)
                        if Decimal(fixed_count + possible_extra) < limit:
                            conflicts.append({
                                'rule': 'Weekend minimum', 'physician': physician,
                                'period_start': start.isoformat(), 'period_end': end.isoformat(),
                                'explanation': f'{physician} can work at most {fixed_count + possible_extra} eligible weekend shifts in this period; the configured minimum is {limit:g}.',
                            })
                    else:
                        bounds['maximum'] = limit if bounds['maximum'] is None else min(bounds['maximum'], limit)
                        if Decimal(fixed_count) > limit:
                            conflicts.append({
                                'rule': 'Weekend maximum', 'physician': physician,
                                'period_start': start.isoformat(), 'period_end': end.isoformat(),
                                'explanation': f'{physician} already has {fixed_count} fixed weekend shifts; the configured maximum is {limit:g}.',
                            })

        # A locked streak beyond a configured maximum cannot be repaired.
        for field, label, dates in (
            ('max_consecutive_weekend_shifts', 'Consecutive weekend shifts', sorted(item.date for item in fixed)),
            ('max_consecutive_weekends', 'Consecutive worked weekends', sorted({item.date - timedelta(days=item.date.weekday()) for item in fixed})),
        ):
            limit = _configured_positive_int(settings, field)
            weight = _configured_positive_penalty(settings, f'{field}_penalty_weight', Decimal('0'))
            if limit is None or weight <= 0:
                continue
            checked_rules += 1
            streak = []
            step = timedelta(days=7 if field.endswith('weekends') else 1)
            for day in dates:
                if streak and day != streak[-1] and day != streak[-1] + step:
                    streak = []
                streak.append(day)
                if len(streak) == limit + 1:
                    conflicts.append({
                        'rule': label, 'physician': physician,
                        'period_start': streak[0].isoformat(), 'period_end': day.isoformat(),
                        'explanation': f'{physician} has more than {limit} locked consecutive weekend {"weeks" if field.endswith("weekends") else "shifts"}.',
                    })

        # If even the optimistic connected component containing a fixed shift
        # is shorter than a configured minimum, no reassignment can repair it.
        candidate_by_date = defaultdict(int)
        for item in weekend_instances:
            fixed_here = physician_id in fixed_weekend[item.id]
            if fixed_here:
                candidate_by_date[item.date] += 1
            if item.facility_id in eligible_facilities:
                candidate_by_date[item.date] += max(
                    item.required_staffing - len(fixed_weekend[item.id]), 0,
                )
        for field, label, unit in (
            ('min_consecutive_weekend_shifts', 'Minimum consecutive weekend shifts', timedelta(days=1)),
            ('min_consecutive_weekends', 'Minimum consecutive worked weekends', timedelta(days=7)),
        ):
            limit = _configured_positive_int(settings, field)
            weight = _configured_positive_penalty(settings, f'{field}_penalty_weight', Decimal('0'))
            if limit is None or weight <= 0 or not fixed:
                continue
            checked_rules += 1
            if unit.days == 1:
                potential = candidate_by_date
                fixed_keys = {item.date for item in fixed}
            else:
                potential = {
                    day - timedelta(days=day.weekday()): 1
                    for day, count in candidate_by_date.items() if count
                }
                fixed_keys = {
                    item.date - timedelta(days=item.date.weekday()) for item in fixed
                }
            component = []

            def report_short_component(days):
                if not days or not fixed_keys.intersection(days):
                    return
                capacity = sum(potential[day] for day in days)
                if capacity < limit:
                    conflicts.append({
                        'rule': label, 'physician': physician,
                        'period_start': days[0].isoformat(),
                        'period_end': days[-1].isoformat(),
                        'explanation': f'{physician} has a fixed weekend assignment here, but at most {capacity} consecutive {"worked weekends" if unit.days == 7 else "weekend shifts"} can fit; the configured minimum is {limit}.',
                    })

            for day in sorted(potential):
                if component and day != component[-1] + unit:
                    report_short_component(component)
                    component = []
                component.append(day)
            report_short_component(component)

        friday_weight = _configured_positive_penalty(
            settings, 'block_friday_night_before_weekend_off_penalty_weight', Decimal('0'),
        )
        if settings.get('block_friday_night_before_weekend_off') and friday_weight > 0:
            checked_rules += 1
            block_start = instances[0].schedule_block.start_date if instances else None
            block_end = instances[0].schedule_block.end_date if instances else None
            for friday_instance in instances:
                friday = friday_instance.date
                if (friday.weekday() != 4 or not friday_instance.shift_template.night_shift
                        or physician_id not in fixed_by_instance[friday_instance.id]
                        or block_start is None or friday < block_start
                        or friday + timedelta(days=2) > block_end):
                    continue
                weekend_dates = {friday + timedelta(days=1), friday + timedelta(days=2)}
                if any(candidate_by_date.get(day, 0) for day in weekend_dates):
                    continue
                conflicts.append({
                    'rule': 'Friday night before weekend off', 'physician': physician,
                    'period_start': friday.isoformat(),
                    'period_end': (friday + timedelta(days=2)).isoformat(),
                    'explanation': f'{physician} has a fixed Friday night, but no eligible weekend-designated Saturday or Sunday shift can follow it.',
                })

    for (period, start, end), bounds_by_physician in period_bounds.items():
        window = [item for item in weekend_instances if start <= item.date <= end]
        required = sum(item.required_staffing for item in window)
        fixed_count = sum(min(len(fixed_weekend[item.id]), item.required_staffing) for item in window)
        remaining = max(required - fixed_count, 0)
        minimum_remaining = sum(max(row['minimum'] - row['fixed'], Decimal('0')) for row in bounds_by_physician.values())
        if minimum_remaining > remaining:
            conflicts.append({
                'rule': 'Combined weekend minimums', 'physician': None,
                'period_start': start.isoformat(), 'period_end': end.isoformat(),
                'explanation': f'{minimum_remaining:g} additional weekend assignments are needed for configured minimums, but only {remaining} slots remain.',
            })
        if len(bounds_by_physician) == len(optimizer_rows) and all(
            row['maximum'] is not None for row in bounds_by_physician.values()
        ):
            maximum_remaining = sum(max(row['maximum'] - row['fixed'], Decimal('0')) for row in bounds_by_physician.values())
            if Decimal(remaining) > maximum_remaining:
                conflicts.append({
                    'rule': 'Combined weekend maximums', 'physician': None,
                    'period_start': start.isoformat(), 'period_end': end.isoformat(),
                    'explanation': f'{remaining} weekend slots remain, but configured maximums allow only {maximum_remaining:g} more assignments.',
                })

    return {
        'status': 'conflict_proven' if conflicts else 'no_conflict_found',
        'weekend_shift_slots': sum(item.required_staffing for item in weekend_instances),
        'fixed_weekend_shifts': sum(
            min(len(fixed_weekend[item.id]), item.required_staffing)
            for item in weekend_instances
        ),
        'checked_rule_count': checked_rules,
        'conflicts': conflicts,
        'interpretation': (
            f'{len(conflicts)} unavoidable weekend-rule conflict(s) found.' if conflicts
            else 'No unavoidable conflict found by the weekend capacity and locked-streak checks.'
        ),
        'scope_note': (
            'Uses Shift Builder weekend designations and only positively weighted contract rules. '
            'Locked assignments and manual-only Shift On requests count as fixed; manual-only users add no flexible capacity. '
            'A clear result does not prove a penalty-free schedule: rest, overlap, requests, and interactions '
            'between rules require the full optimizer.'
        ),
    }


def _night_limit_rows(contract, instances, assigned_instances):
    settings = contract.night_settings if isinstance(contract.night_settings, dict) else {}
    rows = []
    for rule in settings.get('period_rules') or []:
        if not isinstance(rule, dict):
            continue
        maximum = _decimal_or_none(rule.get('max_shifts'))
        if maximum is None:
            continue
        period_type = rule.get('period_type') or 'SCHEDULE_BLOCK'
        for window_start, window_end in _period_windows(instances, period_type):
            count = sum(
                instance.shift_template.night_shift
                and window_start <= instance.date <= window_end
                for instance in assigned_instances
            )
            rows.append({
                'period': period_type,
                'period_start': window_start.isoformat(),
                'period_end': window_end.isoformat(),
                'current_night_count': count,
                'night_max': int(maximum),
            })
    return rows


def _under_minimum_candidate_diagnostic(
    *, physician, contract, instances, assigned_instances, assignment_rows,
    workload_rule_rows, deficit_hours, requests_by_physician_date, locked_open_ids,
):
    rejection_counts = {
        'overlap': 0,
        'rest': 0,
        'request_conflict': 0,
        'night_maximum': 0,
        'facility_ineligible': 0,
        'would_exceed_max_workload': 0,
        'locked_assignment': 0,
        'already_assigned': 0,
    }
    assigned_ids = {instance.id for instance in assigned_instances}
    assigned_intervals = [
        (instance.start_datetime, instance.end_datetime)
        for instance in assigned_instances
    ]
    eligible_facility_ids = set(contract.facilities.values_list('id', flat=True))
    minimum_rest = _minimum_rest_hours(contract)
    night_limits = _night_limit_rows(contract, instances, assigned_instances)
    assignments_by_instance = defaultdict(list)
    for assignment in assignment_rows:
        assignments_by_instance[assignment.shift_instance_id].append(assignment)

    legal_candidates = []
    request_conflicts = []
    for instance in instances:
        reasons = []
        current_rows = assignments_by_instance[instance.id]
        if instance.id in assigned_ids:
            reasons.append('already_assigned')
        if instance.facility_id not in eligible_facility_ids:
            reasons.append('facility_ineligible')
        if _overlaps(instance, assigned_intervals):
            reasons.append('overlap')
        elif _rest_violation(instance, assigned_intervals, minimum_rest):
            reasons.append('rest')

        matching_requests = [
            request for request in _requests_for_shift(
                requests_by_physician_date, physician.id, instance,
            )
            if request.request_type in {
                ScheduleRequest.RequestType.DAY_OFF,
                ScheduleRequest.RequestType.SHIFT_OFF,
            }
        ]
        if matching_requests:
            reasons.append('request_conflict')
            request_conflicts.extend({
                'request_id': request.id,
                'date': request.date.isoformat(),
                'request_type': request.request_type,
                'weight': request.weight,
                'shift_instance_id': instance.id,
            } for request in matching_requests)

        if instance.shift_template.night_shift:
            for limit in night_limits:
                if (
                    limit['period_start'] <= instance.date.isoformat() <= limit['period_end']
                    and limit['current_night_count'] + 1 > limit['night_max']
                ):
                    reasons.append('night_maximum')
                    break

        shift_hours = _shift_hours(instance)
        for rule_row in workload_rule_rows:
            maximum = rule_row['effective_max_hours']
            if maximum is None:
                continue
            if not (rule_row['period_start'] <= instance.date.isoformat() <= rule_row['period_end']):
                continue
            if Decimal(str(rule_row['assigned_hours'])) + shift_hours > Decimal(str(maximum)):
                reasons.append('would_exceed_max_workload')
                break

        is_open_slot = len(current_rows) < instance.required_staffing
        replacement = None
        if instance.id in locked_open_ids:
            reasons.append('locked_assignment')
        elif not is_open_slot:
            replacement = next((row for row in current_rows if not row.is_locked), None)
            if replacement is None:
                reasons.append('locked_assignment')

        reasons = list(dict.fromkeys(reasons))
        if reasons:
            for reason in reasons:
                rejection_counts[reason] += 1
            continue

        legal_candidates.append({
            'shift_instance_id': instance.id,
            'shift_name': instance.shift_template.generated_name(),
            'date': instance.date.isoformat(),
            'start_datetime': instance.start_datetime.isoformat(),
            'end_datetime': instance.end_datetime.isoformat(),
            'facility': instance.facility.short_name or instance.facility.name,
            'hours': _number(shift_hours),
            'night_shift': instance.shift_template.night_shift,
            'action': 'assign_open_slot' if is_open_slot else 'replace_unlocked_assignment',
            'replace_assignment_id': replacement.id if replacement else None,
            'replace_physician_id': replacement.physician_id if replacement else None,
            'replace_physician': (
                _physician_display_name(replacement.physician) if replacement else None
            ),
        })

    legal_candidates.sort(key=lambda row: (
        abs(Decimal(str(row['hours'])) - deficit_hours),
        row['date'],
        row['start_datetime'],
        row['shift_instance_id'],
    ))
    request_conflicts_by_id = {}
    for row in request_conflicts:
        grouped = request_conflicts_by_id.setdefault(row['request_id'], {
            'request_id': row['request_id'],
            'date': row['date'],
            'request_type': row['request_type'],
            'weight': row['weight'],
            'affected_candidate_count': 0,
            'shift_instance_ids': [],
        })
        grouped['affected_candidate_count'] += 1
        if len(grouped['shift_instance_ids']) < 20:
            grouped['shift_instance_ids'].append(row['shift_instance_id'])
    best_candidate_hours = (
        Decimal(str(legal_candidates[0]['hours'])) if legal_candidates else Decimal('0')
    )
    return {
        'current_night_count': sum(
            instance.shift_template.night_shift for instance in assigned_instances
        ),
        'night_max': (
            night_limits[0]['night_max'] if len(night_limits) == 1 else None
        ),
        'night_limit_periods': night_limits,
        'request_conflicts': list(request_conflicts_by_id.values()),
        'legal_candidate_count': len(legal_candidates),
        'legal_candidate_shifts': legal_candidates[:20],
        'legal_candidates_truncated': len(legal_candidates) > 20,
        'best_single_candidate_hours': _number(best_candidate_hours) if legal_candidates else None,
        'remaining_deficit_after_best_single_candidate_hours': (
            _number(max(deficit_hours - best_candidate_hours, Decimal('0')))
            if legal_candidates else _number(deficit_hours)
        ),
        'rejected_candidate_counts_by_reason': rejection_counts,
        'rejection_counts_may_overlap': True,
        'assessment': (
            'allocation-related' if legal_candidates else 'constraint-blocked'
        ),
        'assessment_explanation': (
            'At least one individual shift could be assigned or transferred without the listed conflicts.'
            if legal_candidates else
            'No individual shift could be assigned or transferred without a listed conflict.'
        ),
        'scope_note': (
            'This checks individual open-slot assignments and one-for-one replacement of an unlocked owner only; '
            'request conflicts are treated as candidate rejections even though requests remain soft preferences; '
            'it does not score schedules, backfill displaced owners, or evaluate global swaps.'
        ),
    }


def build_workload_feasibility(
    version, optimizer_run=None, *, include_individual_diagnostics=True,
):
    """Build a read-only, linear-time workload-hours capacity diagnostic."""
    block = version.schedule_block
    instances = list(
        version.shift_instances.filter(
            date__gte=block.start_date,
            date__lte=block.end_date,
        ).select_related(
            'schedule_block', 'shift_template__facility', 'facility',
        ).order_by('date', 'id')
    )
    available_hours = sum(
        (_shift_hours(instance) * instance.required_staffing for instance in instances),
        Decimal('0'),
    )
    required_slots = sum(instance.required_staffing for instance in instances)

    contract_assignments = list(
        ContractUserAssignment.objects.filter(
            domain=version.domain,
            contract__active=True,
            physician__active=True,
        )
        .select_related('contract', 'physician__user')
        .prefetch_related('contract__facilities')
        .order_by('physician__display_name', 'physician__user__last_name', 'physician_id')
    )
    manual_only_physician_ids = {
        assignment.physician_id
        for assignment in contract_assignments
        if assignment.contract.manual_assignment_only
    }
    optimizer_contract_assignments = [
        assignment for assignment in contract_assignments
        if assignment.physician_id not in manual_only_physician_ids
    ]

    detailed_run = optimizer_run if include_individual_diagnostics else None
    assignment_accounting, assignments = _assignment_accounting(
        version, instances, detailed_run,
    )
    visible_assignments = list(
        assignments_for_viewed_run(version, optimizer_run)
        .select_related('shift_instance__shift_template', 'physician__user')
    )
    schedule_requests = list(
        ScheduleRequest.objects.filter(
            schedule_block=block,
            date__gte=block.start_date,
            date__lte=block.end_date,
        ).prefetch_related('shift_templates')
    )
    manual_only_fixed_coverage = _manual_only_fixed_coverage_by_instance(
        instances, visible_assignments, manual_only_physician_ids,
        schedule_requests,
    )
    manual_only_fixed_hours = sum(
        (
            _shift_hours(instance)
            * manual_only_fixed_coverage.get(instance.id, 0)
            for instance in instances
        ),
        Decimal('0'),
    )
    manual_only_fixed_slots = sum(manual_only_fixed_coverage.values())
    optimizer_required_hours = max(
        available_hours - manual_only_fixed_hours, Decimal('0'),
    )
    optimizer_required_slots = max(required_slots - manual_only_fixed_slots, 0)
    physician_count = len(optimizer_contract_assignments)
    default_hours = (
        optimizer_required_hours / physician_count
        if physician_count else Decimal('0')
    )
    default_shifts = (
        Decimal(optimizer_required_slots) / physician_count
        if physician_count else Decimal('0')
    )
    assigned_instances = defaultdict(list)
    if detailed_run is not None:
        for assignment in assignments:
            assigned_instances[assignment.physician_id].append(assignment.shift_instance)
    requests_by_physician_date = defaultdict(list)
    if detailed_run is not None:
        for schedule_request in schedule_requests:
            requests_by_physician_date[
                (schedule_request.physician_id, schedule_request.date)
            ].append(schedule_request)
    locked_open_ids = set(optimizer_run.locked_open_shift_instance_ids or []) if optimizer_run else set()

    physician_rows = []
    aggregate_min = Decimal('0')
    aggregate_max = Decimal('0')
    aggregate_max_unbounded = False
    physicians_without_hour_ranges = []

    for contract_assignment in optimizer_contract_assignments:
        physician = contract_assignment.physician
        contract = contract_assignment.contract
        target = _version_contract_target(version, physician.id, contract, default_hours, default_shifts)
        rule_rows = []
        physician_min = Decimal('0')
        physician_max = Decimal('0')
        physician_max_unbounded = False
        assigned_total = sum(
            (_shift_hours(instance) for instance in assigned_instances[physician.id]),
            Decimal('0'),
        )

        for rule in target.get('rules') or []:
            for window_start, window_end in _period_windows(instances, rule['period_type']):
                effective = _effective_workload_rule(rule, window_start, window_end)
                if effective['units'] != 'HOURS':
                    continue
                assigned_window = sum(
                    (
                        _shift_hours(instance)
                        for instance in assigned_instances[physician.id]
                        if window_start <= instance.date <= window_end
                    ),
                    Decimal('0'),
                )
                minimum = effective['min_value']
                maximum = effective['max_value']
                deficit = max((minimum or Decimal('0')) - assigned_window, Decimal('0'))
                surplus = (
                    max(assigned_window - maximum, Decimal('0'))
                    if maximum is not None else Decimal('0')
                )
                contribution = (
                    deficit * effective['min_penalty_weight']
                    + surplus * effective['max_penalty_weight']
                )
                rule_rows.append({
                    'period': effective['period_type'],
                    'period_start': window_start.isoformat(),
                    'period_end': window_end.isoformat(),
                    'effective_min_hours': _number(minimum),
                    'effective_max_hours': _number(maximum),
                    'assigned_hours': _number(assigned_window) if detailed_run else None,
                    'deficit_hours': _number(deficit) if detailed_run else None,
                    'surplus_hours': _number(surplus) if detailed_run else None,
                    'workload_score_contribution': _number(contribution) if detailed_run else None,
                })

        physician_min, physician_max = _aggregate_hour_rule_bounds(rule_rows)
        physician_max_unbounded = physician_max is None
        name = _physician_display_name(physician)
        if not rule_rows:
            physicians_without_hour_ranges.append(name)
        aggregate_min += physician_min
        if physician_max_unbounded or not rule_rows:
            aggregate_max_unbounded = True
        else:
            aggregate_max += physician_max
        score_contribution = sum(
            (Decimal(str(row['workload_score_contribution'])) for row in rule_rows),
            Decimal('0'),
        ) if detailed_run else None
        physician_row = {
            'physician_id': physician.id,
            'physician': name,
            'fte': _number(physician.fte),
            'contract': contract.name,
            'workload_rule_periods': rule_rows,
            'effective_min_hours': _number(physician_min) if rule_rows else None,
            'effective_max_hours': (
                None if physician_max_unbounded or not rule_rows else _number(physician_max)
            ),
            'assigned_hours': _number(assigned_total) if detailed_run else None,
            'deficit_hours': (
                sum((row['deficit_hours'] for row in rule_rows), 0.0)
                if detailed_run and rule_rows else None
            ),
            'surplus_hours': (
                sum((row['surplus_hours'] for row in rule_rows), 0.0)
                if detailed_run and rule_rows else None
            ),
            'workload_score_contribution': _number(score_contribution),
        }
        deficit_total = Decimal(str(physician_row['deficit_hours'] or 0))
        if detailed_run is not None and deficit_total > 0:
            physician_row['under_minimum_diagnostic'] = _under_minimum_candidate_diagnostic(
                physician=physician,
                contract=contract,
                instances=instances,
                assigned_instances=assigned_instances[physician.id],
                assignment_rows=assignments,
                workload_rule_rows=rule_rows,
                deficit_hours=deficit_total,
                requests_by_physician_date=requests_by_physician_date,
                locked_open_ids=locked_open_ids,
            )
        physician_rows.append(physician_row)

    total_max = None if aggregate_max_unbounded else aggregate_max
    if optimizer_required_hours < aggregate_min:
        status = 'minimum_infeasible'
        interpretation = 'Minimum requirements are collectively infeasible.'
    elif total_max is not None and optimizer_required_hours > total_max:
        status = 'maximum_infeasible'
        interpretation = 'Maximum requirements are collectively infeasible.'
    else:
        status = 'aggregate_feasible'
        interpretation = (
            'Aggregate workload is feasible, but individual feasibility may still be blocked '
            'by nights, rest, requests, eligibility, or locks.'
        )

    bounded_maxima = [
        row['effective_max_hours']
        for row in physician_rows
        if row['effective_max_hours'] is not None
    ]
    cohort_maximum = max(bounded_maxima, default=None)
    reduced_rows = [
        row for row in physician_rows
        if row['fte'] < 1
        or 'turner' in row['physician'].lower()
        or (
            cohort_maximum is not None
            and row['effective_max_hours'] is not None
            and row['effective_max_hours'] < cohort_maximum
        )
    ]
    under_minimum_rows = [
        row for row in physician_rows
        if row.get('under_minimum_diagnostic') is not None
    ]
    adjustment_preview = _fte_adjustment_preview(
        status,
        physician_rows,
        optimizer_required_hours,
        aggregate_min,
        total_max,
    )
    night_feasibility = _night_feasibility(
        version, instances, contract_assignments, optimizer_run,
        manual_only_fixed_coverage,
    )
    request_off_rows = [
        schedule_request for schedule_request in schedule_requests
        if schedule_request.request_type in {
            ScheduleRequest.RequestType.DAY_OFF,
            ScheduleRequest.RequestType.SHIFT_OFF,
        }
    ]
    request_off_feasibility = _request_off_feasibility(
        instances, contract_assignments, request_off_rows,
        manual_only_fixed_coverage,
    )
    weekend_feasibility = _weekend_feasibility(
        instances, contract_assignments, visible_assignments, schedule_requests,
    )
    if adjustment_preview:
        groups = defaultdict(lambda: {'physician_count': 0})
        rate = Decimal(str(adjustment_preview['adjustment_hours_per_fte'] or 0))
        for proposal in adjustment_preview['proposals']:
            groups[Decimal(str(proposal['fte']))]['physician_count'] += 1
        adjustment_preview['fte_groups'] = [
            {
                'fte': _number(fte),
                'physician_count': group['physician_count'],
                'adjustment_hours_per_physician': _number(fte * rate),
            }
            for fte, group in sorted(groups.items(), reverse=True)
        ]
    return {
        'schedule_block': {
            'schedule_block_id': block.id,
            'start_date': block.start_date.isoformat(),
            'end_date': block.end_date.isoformat(),
            'number_of_days': (block.end_date - block.start_date).days + 1,
            'domain': version.domain.name,
            'schedule_version_id': version.id,
            'total_generated_shift_instances': len(instances),
            'total_generated_required_hours': _number(available_hours),
            'optimizer_run_id': optimizer_run.id if optimizer_run else None,
            'total_run_assigned_hours': (
                assignment_accounting['total_run_assigned_hours'] if assignment_accounting else None
            ),
            'total_visible_assigned_hours': (
                assignment_accounting['total_visible_assigned_hours'] if assignment_accounting else None
            ),
            'total_unfilled_hours': (
                assignment_accounting['total_unfilled_hours'] if assignment_accounting else None
            ),
            'assignment_count': (
                assignment_accounting['assignment_count'] if assignment_accounting else None
            ),
            'run_assignment_count': (
                assignment_accounting['run_assignment_count'] if assignment_accounting else None
            ),
            'visible_assignment_count': (
                assignment_accounting['visible_assignment_count'] if assignment_accounting else None
            ),
            'duplicate_shift_assignment_count': (
                assignment_accounting['duplicate_shift_assignment_count'] if assignment_accounting else None
            ),
            'selected_run_duplicate_assignment_count': (
                assignment_accounting['selected_run_duplicate_assignment_count']
                if assignment_accounting else None
            ),
        },
        'assignment_accounting': assignment_accounting,
        'physicians': physician_rows,
        'under_minimum_physicians': under_minimum_rows,
        'aggregate_feasibility': {
            'sum_effective_minimum_hours': _number(aggregate_min),
            'sum_effective_maximum_hours': _number(total_max),
            'total_available_scheduled_hours': _number(optimizer_required_hours),
            'total_generated_required_hours': _number(available_hours),
            'manual_only_fixed_hours': _number(manual_only_fixed_hours),
            'manual_only_fixed_shift_slots': manual_only_fixed_slots,
            'manual_only_physician_count': len(manual_only_physician_ids),
            'available_minus_total_minimum': _number(
                optimizer_required_hours - aggregate_min
            ),
            'total_maximum_minus_available': (
                _number(total_max - optimizer_required_hours)
                if total_max is not None else None
            ),
            'status': status,
            'interpretation': interpretation,
            'physicians_without_hour_ranges': physicians_without_hour_ranges,
            'fte_adjustment_preview': adjustment_preview,
            'has_workload_hour_overrides': bool(version.workload_hour_overrides),
        },
        'night_feasibility': night_feasibility,
        'request_off_feasibility': request_off_feasibility,
        'weekend_feasibility': weekend_feasibility,
        'reduced_contract_focus': reduced_rows,
    }
