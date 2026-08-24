from collections import Counter, defaultdict
from decimal import Decimal

from . import optimizer
from .models import ContractUserAssignment, ScheduleRequest, ScheduleShiftAssignment


def _copy_state(state):
    copied = defaultdict(list)
    for instance_id, physician_ids in state.items():
        copied[instance_id] = list(physician_ids)
    return copied


def _context(version, optimizer_run):
    instances = list(
        optimizer._version_shift_instances_queryset(version)
        .select_related('facility', 'shift_template')
        .order_by('date', 'facility__name', 'start_datetime', 'id')
    )
    assignments = list(
        optimizer._assignments_for_optimizer_run(version, optimizer_run)
        .select_related('shift_instance', 'physician__user')
    )
    contract_assignments = list(
        ContractUserAssignment.objects.filter(
            domain=version.domain, contract__active=True, physician__active=True,
        )
        .select_related('physician__user', 'contract')
        .prefetch_related('contract__facilities')
    )
    physicians = [row.physician for row in contract_assignments]
    contracts = {row.physician_id: row.contract for row in contract_assignments}
    eligible = {
        row.physician_id: {facility.id for facility in row.contract.facilities.all()}
        for row in contract_assignments
    }
    minimum_rest = {
        row.physician_id: optimizer._minimum_rest_hours(row.contract)
        for row in contract_assignments
    }
    total_hours = sum(
        optimizer._shift_hours(instance) * instance.required_staffing
        for instance in instances
    )
    total_slots = sum(instance.required_staffing for instance in instances)
    divisor = Decimal(len(physicians)) if physicians else Decimal('1')
    targets = {
        physician.id: optimizer._contract_target(
            contracts[physician.id], total_hours / divisor, Decimal(total_slots) / divisor,
        )
        for physician in physicians
    }
    requests = defaultdict(list)
    for request in ScheduleRequest.objects.filter(
        schedule_block=version.schedule_block,
        date__range=(version.schedule_block.start_date, version.schedule_block.end_date),
    ).prefetch_related('shift_templates'):
        requests[(request.physician_id, request.date)].append(request)
    state, manual_pairs = optimizer._state_from_assignments(assignments)
    return {
        'instances': instances,
        'instances_by_id': {instance.id: instance for instance in instances},
        'assignments': assignments,
        'physicians': physicians,
        'physicians_by_id': {physician.id: physician for physician in physicians},
        'contracts': contracts,
        'eligible': eligible,
        'minimum_rest': minimum_rest,
        'targets': targets,
        'requests': requests,
        'state': state,
        'manual_pairs': manual_pairs,
    }


def _score(context, state):
    return optimizer._score_schedule(
        context['instances'], context['physicians'], state, context['targets'],
        context['contracts'], context['requests'], context['eligible'],
        context['minimum_rest'],
    )


def _physician_name(context, physician_id):
    physician = context['physicians_by_id'].get(physician_id)
    return optimizer._physician_display_name(physician) if physician else str(physician_id)


def _shift_payload(instance):
    return {
        'shift_instance_id': instance.id,
        'date': instance.date.isoformat(),
        'shift_template_id': instance.shift_template_id,
        'shift_template': str(instance.shift_template),
        'facility_id': instance.facility_id,
        'facility': str(instance.facility),
        'start_datetime': instance.start_datetime.isoformat(),
        'end_datetime': instance.end_datetime.isoformat(),
        'hours': float(optimizer._shift_hours(instance)),
        'night_shift': instance.shift_template.night_shift,
    }


def _hard_rejection(context, state, instance, physician_id, exclude_instance_id):
    if instance.facility_id not in context['eligible'].get(physician_id, set()):
        return 'not_facility_eligible'
    intervals = optimizer._intervals_for_physician(
        state, context['instances_by_id'], physician_id,
        exclude_instance_id=exclude_instance_id,
    )
    if optimizer._overlaps(instance, intervals):
        return 'overlap'
    if optimizer._rest_violation(
        instance, intervals, context['minimum_rest'][physician_id],
    ):
        return 'rest_violation'
    return None


def _scored_result(current_scoring, trial_scoring):
    delta = trial_scoring['score'] - current_scoring['score']
    reasons = []
    if trial_scoring['breakdown']['night_score'] > current_scoring['breakdown']['night_score']:
        reasons.append('would_create_or_worsen_night_violation')
    if trial_scoring['breakdown']['workload_score'] > current_scoring['breakdown']['workload_score']:
        reasons.append('would_create_or_worsen_workload_violation')
    if delta >= 0:
        reasons.append('would_not_lower_total_score')
    return {
        'legal': True,
        'improving': delta < 0,
        'score_delta': float(delta),
        'resulting_total_score': float(trial_scoring['score']),
        'workload_score_delta': float(
            trial_scoring['breakdown']['workload_score']
            - current_scoring['breakdown']['workload_score']
        ),
        'night_score_delta': float(
            trial_scoring['breakdown']['night_score']
            - current_scoring['breakdown']['night_score']
        ),
        'rejection_reasons': reasons,
    }


def _single_move(context, current_scoring, instance_id, from_id, to_id):
    instance = context['instances_by_id'][instance_id]
    base = {
        'action': 'move',
        'shift': _shift_payload(instance),
        'from_physician_id': from_id,
        'from_physician': _physician_name(context, from_id),
        'candidate_physician_id': to_id,
        'candidate_physician': _physician_name(context, to_id),
    }
    if instance_id in set(context['optimizer_run'].locked_open_shift_instance_ids or []):
        return {**base, 'legal': False, 'improving': False, 'score_delta': None,
                'rejection_reasons': ['locked_open']}
    if (instance_id, from_id) in context['manual_pairs']:
        return {**base, 'legal': False, 'improving': False, 'score_delta': None,
                'rejection_reasons': ['locked_assignment']}
    if to_id == from_id or to_id in context['state'][instance_id]:
        return {**base, 'legal': False, 'improving': False, 'score_delta': None,
                'rejection_reasons': ['already_assigned']}
    trial = _copy_state(context['state'])
    optimizer._replace_in_state(trial, instance_id, from_id, to_id)
    rejection = _hard_rejection(context, trial, instance, to_id, instance_id)
    if rejection:
        return {**base, 'legal': False, 'improving': False, 'score_delta': None,
                'rejection_reasons': [rejection]}
    return {**base, **_scored_result(current_scoring, _score(context, trial))}


def _swap(context, current_scoring, left_id, left_physician_id, right_id, right_physician_id):
    left = context['instances_by_id'][left_id]
    right = context['instances_by_id'][right_id]
    base = {
        'action': 'swap',
        'left_shift': _shift_payload(left),
        'left_physician_id': left_physician_id,
        'left_physician': _physician_name(context, left_physician_id),
        'right_shift': _shift_payload(right),
        'right_physician_id': right_physician_id,
        'right_physician': _physician_name(context, right_physician_id),
    }
    result = optimizer.evaluate_plateau_pairwise_swap(
        instances=context['instances'],
        physicians=context['physicians'],
        state=context['state'],
        instances_by_id=context['instances_by_id'],
        manual_pairs=context['manual_pairs'],
        locked_open_instance_ids=set(
            context['optimizer_run'].locked_open_shift_instance_ids or []
        ),
        targets=context['targets'],
        contract_by_physician=context['contracts'],
        requests_by_physician_date=context['requests'],
        eligible_facilities_by_physician=context['eligible'],
        minimum_rest_by_physician=context['minimum_rest'],
        current_score=current_scoring['score'],
        left_instance_id=left_id,
        left_physician_id=left_physician_id,
        right_instance_id=right_id,
        right_physician_id=right_physician_id,
    )
    if not result['legal']:
        return {
            **base, 'legal': False, 'improving': False, 'score_delta': None,
            'rejection_reasons': [result['reason']],
        }
    return {**base, **_scored_result(current_scoring, result['scoring'])}


def build_plateau_explanation(version, optimizer_run):
    report = optimizer.build_violation_report(version, optimizer_run=optimizer_run)
    context = _context(version, optimizer_run)
    context['optimizer_run'] = optimizer_run
    current_scoring = _score(context, context['state'])
    assigned_pairs = [
        (instance_id, physician_id)
        for instance_id, physician_ids in context['state'].items()
        for physician_id in physician_ids
    ]
    violations = []
    all_results = []

    for user in report['users']:
        workload = user.get('workload_score') or {}
        if workload.get('score_contribution', 0) > 0:
            physician_id = user['user_id']
            attempts = []
            swaps = []
            for instance_id, assigned_id in assigned_pairs:
                if assigned_id != physician_id:
                    continue
                for candidate in context['physicians']:
                    result = _single_move(
                        context, current_scoring, instance_id, physician_id, candidate.id,
                    )
                    attempts.append(result)
                    all_results.append(result)
                for right_id, right_physician_id in assigned_pairs:
                    result = _swap(
                        context, current_scoring, instance_id, physician_id,
                        right_id, right_physician_id,
                    )
                    swaps.append(result)
                    all_results.append(result)
            scored_workload_attempts = [
                row for row in [*attempts, *swaps]
                if row.get('legal') and row.get('score_delta') is not None
            ]
            rejected_reasons = Counter(
                reason
                for row in [*attempts, *swaps]
                for reason in row.get('rejection_reasons', [])
            )
            violations.append({
                'category': 'workload',
                'physician_id': physician_id,
                'physician': user['display_name'],
                'rule_violated': workload.get('deviation_direction'),
                'assigned_hours': user['hours'],
                'assigned_shifts': user['shifts'],
                'allowed_min': workload.get('allowed_min'),
                'allowed_max': workload.get('allowed_max'),
                'units': workload.get('allowed_units'),
                'deviation': workload.get('deviation'),
                'penalty': workload.get('score_contribution'),
                'candidate_moves': attempts,
                'candidate_swaps': swaps,
                'candidate_moves_attempted': len(attempts),
                'candidate_swaps_attempted': len(swaps),
                'best_workload_score_delta_found': min(
                    (row['workload_score_delta'] for row in scored_workload_attempts),
                    default=None,
                ),
                'best_total_score_delta_found': min(
                    (row['score_delta'] for row in scored_workload_attempts),
                    default=None,
                ),
                'top_rejected_reasons': [
                    {'reason': reason, 'count': count}
                    for reason, count in rejected_reasons.most_common(5)
                ],
            })

        for violation in user['violations']:
            if not violation.get('penalty_amount'):
                continue
            physician_id = user['user_id']
            involved_ids = violation.get('shift_instance_ids') or []
            moves = []
            swaps = []
            for instance_id in involved_ids:
                if instance_id not in context['instances_by_id']:
                    continue
                for candidate in context['physicians']:
                    result = _single_move(
                        context, current_scoring, instance_id, physician_id, candidate.id,
                    )
                    moves.append(result)
                    all_results.append(result)
                for right_id, right_physician_id in assigned_pairs:
                    result = _swap(
                        context, current_scoring, instance_id, physician_id,
                        right_id, right_physician_id,
                    )
                    swaps.append(result)
                    all_results.append(result)
            legal_deltas = [
                row['score_delta'] for row in [*moves, *swaps]
                if row['legal'] and row['score_delta'] is not None
            ]
            violations.append({
                'category': 'scoring_violation',
                'physician_id': physician_id,
                'physician': user['display_name'],
                'rule_violated': violation['violation_type'],
                'penalty': violation['penalty_amount'],
                'involved_shifts': [
                    _shift_payload(context['instances_by_id'][instance_id])
                    for instance_id in involved_ids
                    if instance_id in context['instances_by_id']
                ],
                'move_attempts': moves,
                'swap_attempts': swaps,
                'best_legal_score_delta': min(legal_deltas) if legal_deltas else None,
            })

    move_deltas = [
        row['score_delta'] for row in all_results
        if row['action'] == 'move' and row['legal'] and row['score_delta'] is not None
    ]
    swap_deltas = [
        row['score_delta'] for row in all_results
        if row['action'] == 'swap' and row['legal'] and row['score_delta'] is not None
    ]
    improving_count = sum(row.get('improving', False) for row in all_results)
    improving_single_count = sum(
        row.get('improving', False) and row['action'] == 'move'
        for row in all_results
    )
    improving_swap_count = sum(
        row.get('improving', False) and row['action'] == 'swap'
        for row in all_results
    )
    conclusion = (
        f'{improving_count} improving move(s) were found in the tested neighborhood.'
        if improving_count else
        'No improving single-shift move or pairwise swap was found under the tested neighborhood. '
        'This supports a local optimum under that move set; it does not prove global impossibility or infeasibility.'
    )
    return {
        'schedule_block_id': version.schedule_block_id,
        'schedule_version_id': version.id,
        'domain': version.domain.name,
        'optimizer_run_id': optimizer_run.id,
        'source_run_mutated': False,
        'total_score': report['total_score'],
        'component_scores': report['score_breakdown'],
        'violations': violations,
        'summary': {
            'tested_neighborhood': 'violation-involved single-shift reassignments and pairwise swaps',
            'single_move_attempts': sum(row['action'] == 'move' for row in all_results),
            'swap_attempts': sum(row['action'] == 'swap' for row in all_results),
            'best_single_move_delta': min(move_deltas) if move_deltas else None,
            'best_pairwise_swap_delta': min(swap_deltas) if swap_deltas else None,
            'legal_improving_moves_found': improving_count,
            'legal_improving_single_moves_found': improving_single_count,
            'legal_improving_pairwise_swaps_found': improving_swap_count,
            'conclusion': conclusion,
        },
    }
