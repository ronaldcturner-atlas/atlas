from collections import defaultdict
from decimal import Decimal

from .models import ContractUserAssignment, ScheduleRequest, ScheduleShiftAssignment
from .optimizer import (
    _assignments_for_optimizer_run,
    _contract_target,
    _decimal_or_none,
    _effective_workload_rule,
    _minimum_rest_hours,
    _overlaps,
    _period_windows,
    _physician_display_name,
    _requests_for_shift,
    _rest_violation,
    _shift_hours,
)


def _number(value):
    return float(value) if value is not None else None


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
        _assignments_for_optimizer_run(version, optimizer_run)
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


def build_workload_feasibility(version, optimizer_run=None):
    """Build a read-only, linear-time workload-hours capacity diagnostic."""
    block = version.schedule_block
    instances = list(
        version.shift_instances.filter(
            date__gte=block.start_date,
            date__lte=block.end_date,
        ).select_related('schedule_block', 'shift_template', 'facility').order_by('date', 'id')
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
    physician_count = len(contract_assignments)
    default_hours = available_hours / physician_count if physician_count else Decimal('0')
    default_shifts = Decimal(required_slots) / physician_count if physician_count else Decimal('0')

    assignment_accounting, assignments = _assignment_accounting(
        version, instances, optimizer_run,
    )
    assigned_instances = defaultdict(list)
    if optimizer_run is not None:
        for assignment in assignments:
            assigned_instances[assignment.physician_id].append(assignment.shift_instance)
    requests_by_physician_date = defaultdict(list)
    if optimizer_run is not None:
        requests = ScheduleRequest.objects.filter(
            schedule_block=block,
            date__gte=block.start_date,
            date__lte=block.end_date,
        ).prefetch_related('shift_templates')
        for schedule_request in requests:
            requests_by_physician_date[
                (schedule_request.physician_id, schedule_request.date)
            ].append(schedule_request)
    locked_open_ids = set(optimizer_run.locked_open_shift_instance_ids or []) if optimizer_run else set()

    physician_rows = []
    aggregate_min = Decimal('0')
    aggregate_max = Decimal('0')
    aggregate_max_unbounded = False
    physicians_without_hour_ranges = []

    for contract_assignment in contract_assignments:
        physician = contract_assignment.physician
        contract = contract_assignment.contract
        target = _contract_target(contract, default_hours, default_shifts)
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
                physician_min += minimum or Decimal('0')
                if maximum is None:
                    physician_max_unbounded = True
                else:
                    physician_max += maximum
                rule_rows.append({
                    'period': effective['period_type'],
                    'period_start': window_start.isoformat(),
                    'period_end': window_end.isoformat(),
                    'effective_min_hours': _number(minimum),
                    'effective_max_hours': _number(maximum),
                    'assigned_hours': _number(assigned_window) if optimizer_run else None,
                    'deficit_hours': _number(deficit) if optimizer_run else None,
                    'surplus_hours': _number(surplus) if optimizer_run else None,
                    'workload_score_contribution': _number(contribution) if optimizer_run else None,
                })

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
        ) if optimizer_run else None
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
            'assigned_hours': _number(assigned_total) if optimizer_run else None,
            'deficit_hours': (
                sum((row['deficit_hours'] for row in rule_rows), 0.0)
                if optimizer_run and rule_rows else None
            ),
            'surplus_hours': (
                sum((row['surplus_hours'] for row in rule_rows), 0.0)
                if optimizer_run and rule_rows else None
            ),
            'workload_score_contribution': _number(score_contribution),
        }
        deficit_total = Decimal(str(physician_row['deficit_hours'] or 0))
        if optimizer_run is not None and deficit_total > 0:
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
    if available_hours < aggregate_min:
        status = 'minimum_infeasible'
        interpretation = 'Minimum requirements are collectively infeasible.'
    elif total_max is not None and available_hours > total_max:
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
            'total_available_scheduled_hours': _number(available_hours),
            'available_minus_total_minimum': _number(available_hours - aggregate_min),
            'total_maximum_minus_available': (
                _number(total_max - available_hours) if total_max is not None else None
            ),
            'status': status,
            'interpretation': interpretation,
            'physicians_without_hour_ranges': physicians_without_hour_ranges,
        },
        'reduced_contract_focus': reduced_rows,
    }
