"""Isolated persistence wrapper for the opt-in Atlas v2 test engine."""

import io
import json
from decimal import Decimal
from time import monotonic

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from apps.domains.models import DomainMembership
from apps.facilities.models import Facility

from .initial_schedule import construct_complete_initial_schedule
from .models import (
    ContractUserAssignment,
    OptimizerRun,
    ScheduleRequest,
    ScheduleShiftAssignment,
)
from .optimizer import build_violation_report
from .optimizer_assignment_snapshot import (
    state_from_assignments,
    version_shift_instances_queryset,
)
from .optimizer_v2 import (
    reassign_assignment,
    rotate_assignments,
    swap_assignments,
)
from .run_state import assignments_for_viewed_run


V2_RUN_KIND = 'OPTIMIZER_V2'
LEGACY_V2_TEST_RUN_KIND = 'OPTIMIZER_V2_TEST'
V2_RUN_KINDS = frozenset({V2_RUN_KIND, LEGACY_V2_TEST_RUN_KIND})
# Compatibility import for older code and completed-run tests.
V2_TEST_RUN_KIND = LEGACY_V2_TEST_RUN_KIND


def _compact_search_summary(search_result):
    """Keep useful completed-run metrics without retaining replay evidence."""
    keys = (
        'stage', 'wall_seconds', 'stopped_reason', 'total_evaluations',
        'unique_evaluated_states', 'unique_accepted_states',
        'accepted_transitions', 'improving_transitions',
        'diversification_transitions', 'primary_improvements',
        'proportionality_improvements', 'predicted_final_score',
        'aggregate_kernel_schedules_per_second', 'restart_count',
        'search_generation', 'consecutive_unproductive_generations',
        'consecutive_exhausted_pipeline_epochs', 'distribution_focus',
    )
    return {
        key: search_result[key]
        for key in keys
        if key in search_result
    }


def _compact_bootstrap_summary(summary):
    if not isinstance(summary, dict):
        return None
    keys = (
        'initial_score', 'final_score', 'runtime_seconds', 'stopped_reason',
        'iterations_run', 'improvement_count', 'assignments_made',
        'unfilled_shift_count', 'final_overlap_violations',
    )
    return {key: summary[key] for key in keys if key in summary}


def _failure_configuration_snapshot(schedule_version):
    """Capture the mutable inputs used to launch one optimizer run."""
    block = schedule_version.schedule_block
    contract_rows = []
    assignments = (
        ContractUserAssignment.objects
        .filter(domain=schedule_version.domain, contract__active=True)
        .select_related('contract')
        .prefetch_related('contract__facilities')
        .order_by('physician_id')
    )
    for assignment in assignments:
        contract = assignment.contract
        contract_rows.append({
            'physician_id': assignment.physician_id,
            'contract_id': contract.id,
            'contract_name': contract.name,
            'manual_assignment_only': contract.manual_assignment_only,
            'facility_ids': list(
                contract.facilities.order_by('id').values_list('id', flat=True)
            ),
            'workload_settings': contract.workload_settings,
            'shift_settings': contract.shift_settings,
            'night_settings': contract.night_settings,
            'weekend_settings': contract.weekend_settings,
            'request_settings': contract.request_settings,
            'contract_updated_at': contract.updated_at.isoformat(),
        })
    request_rows = []
    for request in (
        ScheduleRequest.objects.filter(schedule_block=block)
        .prefetch_related('shift_templates')
        .order_by('id')
    ):
        request_rows.append({
            'id': request.id,
            'physician_id': request.physician_id,
            'date': request.date.isoformat(),
            'request_scope': request.request_scope,
            'request_type': request.request_type,
            'weight': request.weight,
            'shift_template_ids': list(
                request.shift_templates.order_by('id')
                .values_list('id', flat=True)
            ),
            'updated_at': request.updated_at.isoformat(),
        })
    instance_rows = list(
        version_shift_instances_queryset(schedule_version)
        .order_by('id')
        .values(
            'id', 'date', 'shift_template_id', 'facility_id',
            'start_datetime', 'end_datetime', 'required_staffing',
            'is_locked_open', 'split_parent_id',
        )
    )
    for row in instance_rows:
        row['date'] = row['date'].isoformat()
        row['start_datetime'] = row['start_datetime'].isoformat()
        row['end_datetime'] = row['end_datetime'].isoformat()
    facility_rows = list(
        Facility.objects.filter(region_id=schedule_version.domain.region_id)
        .order_by('id')
        .values(
            'id', 'name', 'short_name', 'timezone', 'color', 'active',
            'sort_order',
        )
    )
    template_rows = list(
        schedule_version.domain.shift_templates.order_by('id').values(
            'id', 'facility_id', 'name', 'start_time', 'end_time',
            'active_days_of_week', 'weekend_days', 'night_shift',
            'default_staffing_count', 'active',
        )
    )
    for row in template_rows:
        row['start_time'] = row['start_time'].isoformat()
        row['end_time'] = row['end_time'].isoformat()
    user_rows = []
    memberships = (
        DomainMembership.objects.filter(domain=schedule_version.domain)
        .select_related('user', 'user__physician', 'role_template')
        .order_by('user_id')
    )
    for membership in memberships:
        physician = getattr(membership.user, 'physician', None)
        user_rows.append({
            'user_id': membership.user_id,
            'username': membership.user.username,
            'email': membership.user.email,
            'first_name': membership.user.first_name,
            'last_name': membership.user.last_name,
            'user_active': membership.user.is_active,
            'membership_id': membership.id,
            'membership_role': membership.role,
            'role_template_id': membership.role_template_id,
            'clinically_active': membership.clinically_active,
            'membership_active': membership.active,
            'membership_updated_at': membership.updated_at.isoformat(),
            'physician_id': physician.id if physician else None,
            'display_name': physician.display_name if physician else '',
            'clinician_type': physician.clinician_type if physician else '',
            'fte': str(physician.fte) if physician else None,
            'physician_active': physician.active if physician else False,
            'primary_facility_id': (
                physician.primary_facility_id if physician else None
            ),
        })
    return {
        'captured_at': timezone.now().isoformat(),
        'schedule_version_id': schedule_version.id,
        'schedule_block_id': block.id,
        'domain_id': schedule_version.domain_id,
        'region_id': schedule_version.domain.region_id,
        'organization_id': schedule_version.domain.region.organization_id,
        'block_start_date': block.start_date.isoformat(),
        'block_end_date': block.end_date.isoformat(),
        'shift_template_fingerprint': (
            schedule_version.shift_template_fingerprint
        ),
        'workload_hour_overrides': schedule_version.workload_hour_overrides,
        'facilities': facility_rows,
        'shift_templates': template_rows,
        'users': user_rows,
        'contracts': contract_rows,
        'requests': request_rows,
        'shift_instances': instance_rows,
    }


def _apply_search_operations(state, operations):
    """Replay the ordered V2 transition lineage into an independent state."""
    current = state
    for raw_operation in operations:
        if str(raw_operation).startswith('C:'):
            values = raw_operation.split(':')
            if len(values) != 10:
                raise ValueError(
                    'A V2 rotation operation requires nine integers.'
                )
            numbers = [int(value) for value in values[1:]]
            current = rotate_assignments(
                current,
                [
                    (numbers[0], numbers[1]),
                    (numbers[3], numbers[4]),
                    (numbers[6], numbers[7]),
                ],
                [numbers[2], numbers[5], numbers[8]],
            )
            continue
        if str(raw_operation).startswith('R:'):
            _marker, instance_id, old_physician_id, new_physician_id = (
                raw_operation.split(':')
            )
            current = reassign_assignment(
                current,
                int(instance_id),
                int(old_physician_id),
                int(new_physician_id),
            )
            continue
        values = tuple(int(value) for value in raw_operation.split(':'))
        if len(values) != 4:
            raise ValueError('A V2 swap operation requires four integers.')
        current = swap_assignments(
            current,
            (values[0], values[1]),
            (values[2], values[3]),
        )
    return current


def _require_matching_final_score(final_score, search_result):
    predicted_final_score = search_result.get('predicted_final_score')
    if predicted_final_score is None or abs(
        Decimal(str(final_score)) - Decimal(str(predicted_final_score))
    ) > Decimal('0.0001'):
        raise ValueError(
            'Atlas v2 compiled scoring diverged from the final '
            'authoritative score; the isolated result was not saved.'
        )


def _require_hard_valid_final_report(report):
    breakdown = report['score_breakdown']
    if Decimal(str(breakdown.get('overlap_score', 0))) > 0:
        raise ValueError(
            'Atlas v2 produced a time-overlap conflict; the isolated '
            'result was not saved.'
        )
    if Decimal(str(breakdown.get('invalid_assignment_score', 0))) > 0:
        raise ValueError(
            'Atlas v2 produced an optimizer-ineligible assignment; the '
            'isolated result was not saved.'
        )
    if Decimal(str(breakdown.get('coverage_score', 0))) > 0:
        raise ValueError(
            'Atlas v2 produced an incomplete schedule; the isolated '
            'result was not saved.'
        )


def _authoritative_search_initial_score(search_result, fallback):
    score_checkpoints = search_result.get('score_checkpoints') or []
    if (
        score_checkpoints
        and int(score_checkpoints[0].get('accepted_transitions', -1)) == 0
    ):
        return Decimal(str(score_checkpoints[0]['score']))
    return Decimal(str(fallback))


def _persist_v2_search_failure(
    *, optimizer_run, schedule_version, initial_score, source_report,
    diagnostic_state, started_at, exc,
):
    diagnostic = dict(diagnostic_state or {})
    diagnostic.update({
        'failure_recorded_at': timezone.now().isoformat(),
        'runtime_seconds': monotonic() - started_at,
        'exception_type': type(exc).__name__,
        'exception_message': str(exc),
        'initial_authoritative_score': float(initial_score),
        'initial_score_breakdown': source_report.get('score_breakdown') or {},
    })
    try:
        if 'configuration_snapshot' not in diagnostic:
            diagnostic['configuration_snapshot'] = (
                _failure_configuration_snapshot(schedule_version)
            )
    except Exception as snapshot_exc:  # Preserve the primary optimizer error.
        diagnostic['configuration_snapshot_error'] = {
            'exception_type': type(snapshot_exc).__name__,
            'exception_message': str(snapshot_exc),
        }
    optimizer_run.initial_score = initial_score
    optimizer_run.score_breakdown = source_report.get('score_breakdown') or {}
    optimizer_run.optimizer_summary = {
        'optimizer_engine': 'V2',
        'optimizer_run_id': optimizer_run.id,
        'optimizer_run_number': optimizer_run.run_number,
        'start_mode': optimizer_run.start_mode,
        'initial_score': float(initial_score),
        'runtime_seconds': diagnostic['runtime_seconds'],
        'failure_type': type(exc).__name__,
        'failure_message': str(exc),
        'last_verified_score': (
            (diagnostic.get('last_authoritative_checkpoint') or {}).get(
                'score'
            )
        ),
        'best_verified_score': (
            (diagnostic.get('best_authoritative_checkpoint') or {}).get(
                'score'
            )
        ),
        'predicted_best_score': diagnostic.get('best_predicted_score'),
    }
    optimizer_run.optimizer_debug = {
        'optimizer_engine': 'V2',
        'failure_diagnostic': diagnostic,
    }
    optimizer_run.save(update_fields=[
        'initial_score', 'score_breakdown', 'optimizer_summary',
        'optimizer_debug',
    ])


def optimize_schedule_version_v2(
    schedule_version,
    *,
    optimizer_run,
    source_run,
    created_by=None,
    stop_requested=None,
    progress_callback=None,
):
    """Build or copy an isolated start, run v2, and save one result."""
    if optimizer_run.run_kind not in V2_RUN_KINDS:
        raise ValueError('Atlas V2 requires an Atlas V2 run record.')
    fresh_fill = optimizer_run.start_mode == OptimizerRun.StartMode.FRESH_FILL
    if not fresh_fill:
        if source_run is None or source_run.status != OptimizerRun.Status.COMPLETED:
            raise ValueError('Atlas v2 test requires a completed previous run.')
        if source_run.schedule_version_id != schedule_version.id:
            raise ValueError('Atlas v2 source run belongs to another schedule version.')

    overall_started = monotonic()
    bootstrap_summary = None
    if fresh_fill:
        bootstrap_summary = construct_complete_initial_schedule(
            schedule_version,
            created_by=created_by,
            optimizer_run=optimizer_run,
            stop_requested=stop_requested,
            progress_callback=progress_callback,
        )
        source_run = optimizer_run

    source_report = build_violation_report(
        schedule_version,
        optimizer_run=source_run,
    )
    initial_score = Decimal(str(source_report['total_score']))
    if progress_callback is not None:
        progress_callback(initial_score, force=True)

    source_assignments = list(
        assignments_for_viewed_run(schedule_version, source_run)
        .select_related('shift_instance', 'physician')
        .order_by('shift_instance_id', 'physician_id', 'id')
    )
    launch_snapshot = _failure_configuration_snapshot(schedule_version)
    launch_snapshot['source_assignments'] = [
        {
            'id': assignment.id,
            'shift_instance_id': assignment.shift_instance_id,
            'physician_id': assignment.physician_id,
            'assignment_source': assignment.assignment_source,
            'is_locked': assignment.is_locked,
        }
        for assignment in source_assignments
    ]
    source_rescore = {
        'source_run_id': source_run.id,
        'source_run_number': source_run.run_number,
        'source_stored_score': (
            float(source_run.final_score)
            if source_run.final_score is not None else None
        ),
        'current_authoritative_score': float(initial_score),
        'current_score_breakdown': source_report['score_breakdown'],
    }
    optimizer_run.initial_score = initial_score
    optimizer_run.score_breakdown = source_report['score_breakdown']
    optimizer_run.optimizer_debug = {
        'optimizer_engine': 'V2',
        'launch_snapshot': launch_snapshot,
        'source_rescore': source_rescore,
    }
    optimizer_run.save(update_fields=[
        'initial_score', 'score_breakdown', 'optimizer_debug',
    ])
    has_movable_assignment = any(
        assignment.assignment_source
        != ScheduleShiftAssignment.AssignmentSource.MANUAL
        for assignment in source_assignments
    )

    output = io.StringIO()
    diagnostic_state = {
        'run_id': optimizer_run.id,
        'run_number': optimizer_run.run_number,
        'seed': optimizer_run.seed,
        'start_mode': optimizer_run.start_mode,
        'optimization_focus': optimizer_run.optimization_focus,
        'maximum_runtime_seconds': optimizer_run.max_runtime_seconds,
        'search_started_at': timezone.now().isoformat(),
        'configuration_snapshot': launch_snapshot,
        'source_rescore': source_rescore,
    }
    remaining_runtime_seconds = max(
        float(optimizer_run.max_runtime_seconds) - (monotonic() - overall_started),
        0.0,
    )
    user_stopped = bool(stop_requested is not None and stop_requested())
    if initial_score == 0 and not has_movable_assignment:
        search_result = {
            'stage': 'V2_CONTINUOUS_IMPROVEMENT_CHAIN',
            'swaps': [],
            'wall_seconds': 0.0,
            'stopped_reason': 'complete_fixed_schedule',
            'total_evaluations': 0,
            'accepted_transitions': 0,
            'improving_transitions': 0,
            'neighborhoods': [],
            'predicted_final_score': 0.0,
            'score_checkpoints': [],
        }
    elif remaining_runtime_seconds <= 0 or user_stopped:
        search_result = {
            'stage': 'V2_CONTINUOUS_IMPROVEMENT_CHAIN',
            'swaps': [],
            'wall_seconds': 0.0,
            'stopped_reason': (
                'user_requested' if user_stopped else 'runtime_limit'
            ),
            'total_evaluations': 0,
            'accepted_transitions': 0,
            'neighborhoods': [],
            'predicted_final_score': float(initial_score),
            'score_checkpoints': [],
        }
    else:
        try:
            call_command(
                'benchmark_v2_continuous',
                run_id=source_run.id,
                target_evaluations=10**15,
                max_transitions=100_000,
                max_runtime_seconds=remaining_runtime_seconds,
                configured_runtime_seconds=float(
                    optimizer_run.max_runtime_seconds
                ),
                minimum_rate=0.0,
                stress_contract_count=0,
                # The selected transition is always checked authoritatively by
                # the kernel. Additional sampled candidates are a diagnostic
                # benchmark, not production work.
                validate_sample=0,
                checkpoint_interval=20,
                starting_score=float(initial_score),
                search_seed=int(optimizer_run.seed or optimizer_run.id),
                distribution_focus=(
                    optimizer_run.optimization_focus
                    == OptimizerRun.OptimizationFocus.DISTRIBUTION
                ),
                stop_requested=stop_requested,
                progress_callback=progress_callback,
                diagnostic_state=diagnostic_state,
                as_json=True,
                stdout=output,
            )
        except Exception as exc:
            _persist_v2_search_failure(
                optimizer_run=optimizer_run,
                schedule_version=schedule_version,
                initial_score=initial_score,
                source_report=source_report,
                diagnostic_state=diagnostic_state,
                started_at=overall_started,
                exc=exc,
            )
            raise
        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        if not lines:
            raise ValueError('Atlas v2 test did not return a search result.')
        search_result = json.loads(lines[-1])
        initial_score = _authoritative_search_initial_score(
            search_result, initial_score,
        )

    state, _locked_manual_pairs = state_from_assignments(source_assignments)
    state = _apply_search_operations(state, search_result['swaps'])

    source_by_pair = {}
    for assignment in source_assignments:
        pair = (assignment.shift_instance_id, assignment.physician_id)
        current = source_by_pair.get(pair)
        if current is None or (
            assignment.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.MANUAL
        ):
            source_by_pair[pair] = assignment

    rows = []
    for instance_id in sorted(state):
        for physician_id in sorted(state[instance_id]):
            source = source_by_pair.get((instance_id, physician_id))
            preserve_manual = bool(
                source is not None
                and source.assignment_source
                == ScheduleShiftAssignment.AssignmentSource.MANUAL
            )
            rows.append(ScheduleShiftAssignment(
                shift_instance_id=instance_id,
                physician_id=physician_id,
                created_by=created_by,
                assignment_source=(
                    ScheduleShiftAssignment.AssignmentSource.MANUAL
                    if preserve_manual
                    else ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
                ),
                optimizer_run=optimizer_run,
                is_locked=bool(source.is_locked) if preserve_manual else False,
            ))

    with transaction.atomic():
        ScheduleShiftAssignment.objects.filter(optimizer_run=optimizer_run).delete()
        ScheduleShiftAssignment.objects.bulk_create(rows, batch_size=1000)
        report = build_violation_report(
            schedule_version,
            optimizer_run=optimizer_run,
        )
        final_score = Decimal(str(report['total_score']))
        _require_matching_final_score(final_score, search_result)
        _require_hard_valid_final_report(report)
        if final_score > initial_score + Decimal('0.0001'):
            raise ValueError(
                'Atlas v2 test result worsened the authoritative penalty; '
                'the isolated result was not saved.'
            )
        instance_required = dict(
            version_shift_instances_queryset(schedule_version).values_list(
                'id', 'required_staffing',
            )
        )
        unfilled = sum(
            max(int(required) - len(state.get(instance_id, ())), 0)
            for instance_id, required in instance_required.items()
        )
        summary = {
            'message': 'Atlas V2 run completed.',
            'optimizer_engine': 'V2',
            'optimizer_run_id': optimizer_run.id,
            'optimizer_run_number': optimizer_run.run_number,
            'start_mode': optimizer_run.start_mode,
            'initial_score': float(initial_score),
            'final_score': float(final_score),
            'total_score': float(final_score),
            'score_breakdown': report['score_breakdown'],
            'initial_score_breakdown': source_report['score_breakdown'],
            'source_run_id': source_rescore['source_run_id'],
            'source_run_number': source_rescore['source_run_number'],
            'source_stored_score': source_rescore['source_stored_score'],
            'source_rescored_score': (
                source_rescore['current_authoritative_score']
            ),
            'runtime_seconds': monotonic() - overall_started,
            'timed_out': search_result['stopped_reason'] == 'runtime_limit',
            'stopped_reason': search_result['stopped_reason'],
            'iterations_run': int(search_result['total_evaluations']),
            'improvement_count': int(
                search_result.get(
                    'improving_transitions',
                    search_result['accepted_transitions'],
                )
            ),
            'assignments_made': len(rows),
            'unfilled_shift_count': unfilled,
            'v2_search': _compact_search_summary(search_result),
            'fresh_fill_bootstrap': _compact_bootstrap_summary(
                bootstrap_summary
            ),
        }
        optimizer_run.status = OptimizerRun.Status.COMPLETED
        optimizer_run.initial_score = initial_score
        optimizer_run.final_score = final_score
        optimizer_run.score_breakdown = report['score_breakdown']
        optimizer_run.optimizer_summary = summary
        # Detailed checkpoints and transition replay data are useful only
        # while a run is active or after an unexpected failure.
        optimizer_run.optimizer_debug = {}
        optimizer_run.score_is_stale = False
        optimizer_run.is_active = False
        optimizer_run.notes = (
            'Atlas v2 Fresh Fill result; historical and active schedules were preserved.'
            if fresh_fill
                else 'Atlas V2 result; source and active schedules were preserved.'
        )
        optimizer_run.save(update_fields=[
            'status', 'initial_score', 'final_score', 'score_breakdown',
            'optimizer_summary', 'optimizer_debug', 'score_is_stale',
            'is_active', 'notes',
        ])
    if progress_callback is not None:
        progress_callback(final_score, force=True)
    return summary


# Existing imports remain valid while callers migrate to the product name.
optimize_schedule_version_v2_test = optimize_schedule_version_v2
