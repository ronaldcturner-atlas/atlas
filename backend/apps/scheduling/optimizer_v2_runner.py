"""Isolated persistence wrapper for the opt-in Atlas v2 test engine."""

import io
import json
import hashlib
from decimal import Decimal
from time import monotonic

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Physician
from apps.domains.models import DomainMembership
from apps.facilities.models import Facility

from .initial_schedule import (
    complete_initial_schedule_from_source,
    construct_complete_initial_schedule,
)
from .contract_overrides import apply_effective_contracts
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
from .optimizer_revisions import (
    LEGACY_V2_TEST_RUN_KIND,
    MULTISTART_FRESH_FILL_V2_REVISION,
    STABLE_V2_REVISION,
    V2_RUN_KIND,
    V2_RUN_KINDS,
    code_release_identifier,
    configured_scoring_revision,
    configuration_digest,
    optimizer_provenance,
    require_supported_v2_revision,
)
from .run_state import assignments_for_viewed_run


# Compatibility import for older code and completed-run tests.
V2_TEST_RUN_KIND = LEGACY_V2_TEST_RUN_KIND
MULTISTART_FRESH_FILL_CANDIDATE_COUNT = 6


def _fresh_fill_candidate_seeds(base_seed, candidate_count):
    """Return a deterministic, unique seed sequence led by the run seed."""
    base_seed = int(base_seed)
    seeds = [base_seed]
    maximum_seed = (1 << 63) - 1
    for candidate_index in range(1, int(candidate_count)):
        digest = hashlib.sha256(
            f'atlas-fresh-fill:{base_seed}:{candidate_index}'.encode('utf-8')
        ).digest()
        candidate_seed = int.from_bytes(digest[:8], 'big') & maximum_seed
        if candidate_seed == 0:
            candidate_seed = candidate_index
        while candidate_seed in seeds:
            candidate_seed = (candidate_seed + 1) & maximum_seed
            if candidate_seed == 0:
                candidate_seed = 1
        seeds.append(candidate_seed)
    return tuple(seeds)


def _fresh_fill_candidate_rank(report, candidate_index):
    """Rank by official penalty, then proportionality, then stable order."""
    proportionality = report.get('proportionality') or {}
    return (
        Decimal(str(report['total_score'])),
        Decimal(str(proportionality.get('total', 0))),
        int(candidate_index),
    )


def _construct_multistart_initial_schedule(
    schedule_version,
    *,
    optimizer_run,
    created_by=None,
    stop_requested=None,
):
    """Persist the best of deterministic, independently constructed starts."""
    base_seed = int(optimizer_run.seed or optimizer_run.id)
    seeds = _fresh_fill_candidate_seeds(
        base_seed, MULTISTART_FRESH_FILL_CANDIDATE_COUNT,
    )
    candidates = []
    selected_rank = None
    selected_rows = None
    selected_summary = None
    selected_index = None

    try:
        for candidate_index, candidate_seed in enumerate(seeds):
            if (
                candidate_index > 0
                and stop_requested is not None
                and stop_requested()
            ):
                break
            ScheduleShiftAssignment.objects.filter(
                optimizer_run=optimizer_run,
            ).delete()
            candidate_started = monotonic()
            summary = construct_complete_initial_schedule(
                schedule_version,
                created_by=created_by,
                optimizer_run=optimizer_run,
                seed=candidate_seed,
                stop_requested=stop_requested,
                # Candidate scores are published only after the best complete
                # hard-valid bootstrap has been selected.
                progress_callback=None,
            )
            report = build_violation_report(
                schedule_version,
                optimizer_run=optimizer_run,
            )
            _require_hard_valid_final_report(report)
            rows = list(
                ScheduleShiftAssignment.objects
                .filter(optimizer_run=optimizer_run)
                .order_by('shift_instance_id', 'physician_id', 'id')
                .values(
                    'shift_instance_id',
                    'physician_id',
                    'created_by_id',
                    'assignment_source',
                    'is_locked',
                )
            )
            rank = _fresh_fill_candidate_rank(report, candidate_index)
            candidate = {
                'candidate_index': candidate_index,
                'seed': candidate_seed,
                'score': float(report['total_score']),
                'proportionality': float(
                    (report.get('proportionality') or {}).get('total', 0)
                ),
                'score_breakdown': report['score_breakdown'],
                'assignment_count': len(rows),
                'wall_seconds': monotonic() - candidate_started,
            }
            candidates.append(candidate)
            if selected_rank is None or rank < selected_rank:
                selected_rank = rank
                selected_rows = rows
                selected_summary = dict(summary)
                selected_index = candidate_index
    except Exception:
        if selected_rows is not None:
            ScheduleShiftAssignment.objects.filter(
                optimizer_run=optimizer_run,
            ).delete()
            ScheduleShiftAssignment.objects.bulk_create([
                ScheduleShiftAssignment(
                    optimizer_run=optimizer_run,
                    **row,
                )
                for row in selected_rows
            ], batch_size=1000)
        raise

    if selected_rows is None:
        raise ValueError('Multi-start Fresh Fill did not produce a candidate.')

    ScheduleShiftAssignment.objects.filter(
        optimizer_run=optimizer_run,
    ).delete()
    ScheduleShiftAssignment.objects.bulk_create([
        ScheduleShiftAssignment(
            optimizer_run=optimizer_run,
            **row,
        )
        for row in selected_rows
    ], batch_size=1000)
    optimizer_run.refresh_from_db()
    selected = candidates[selected_index]
    return {
        **selected_summary,
        'final_score': selected['score'],
        'multi_start': {
            'strategy': 'deterministic_best_of_n',
            'base_search_seed': base_seed,
            'requested_candidate_count': len(seeds),
            'completed_candidate_count': len(candidates),
            'selected_candidate_index': selected_index,
            'selected_seed': selected['seed'],
            'selected_score': selected['score'],
            'selected_proportionality': selected['proportionality'],
            'candidates': candidates,
        },
    }


def _compact_search_summary(search_result):
    """Keep useful completed-run metrics and bounded score audit evidence."""
    keys = (
        'stage', 'optimizer_revision', 'wall_seconds', 'stopped_reason',
        'total_evaluations',
        'unique_evaluated_states', 'unique_accepted_states',
        'accepted_transitions', 'improving_transitions',
        'diversification_transitions', 'primary_improvements',
        'proportionality_improvements', 'predicted_final_score',
        'score_checkpoint_interval',
        'aggregate_kernel_schedules_per_second', 'restart_count',
        'search_generation', 'consecutive_unproductive_generations',
        'consecutive_exhausted_pipeline_epochs', 'distribution_focus',
        'production_telemetry',
    )
    compact = {
        key: search_result[key]
        for key in keys
        if key in search_result
    }
    checkpoints = list(search_result.get('score_checkpoints') or [])
    if checkpoints:
        maximum_checkpoint_count = 256
        if len(checkpoints) > maximum_checkpoint_count:
            compact['score_checkpoints'] = [
                checkpoints[0],
                *checkpoints[-(maximum_checkpoint_count - 1):],
            ]
            compact['score_checkpoints_omitted'] = (
                len(checkpoints) - maximum_checkpoint_count
            )
        else:
            compact['score_checkpoints'] = checkpoints
    best_checkpoint = search_result.get('best_authoritative_checkpoint')
    if isinstance(best_checkpoint, dict):
        compact['best_authoritative_checkpoint'] = {
            key: best_checkpoint[key]
            for key in (
                'accepted_transitions', 'score', 'score_breakdown',
                'elapsed_seconds',
                'schedule_fingerprint', 'epoch', 'restart_mode',
                'selection_mode',
                'operation_count',
            )
            if key in best_checkpoint
        }
    return compact


def _compact_bootstrap_summary(summary):
    if not isinstance(summary, dict):
        return None
    keys = (
        'initial_score', 'final_score', 'runtime_seconds', 'stopped_reason',
        'iterations_run', 'improvement_count', 'assignments_made',
        'unfilled_shift_count', 'final_overlap_violations',
    )
    compact = {key: summary[key] for key in keys if key in summary}
    if isinstance(summary.get('multi_start'), dict):
        compact['multi_start'] = summary['multi_start']
    return compact


def _failure_configuration_snapshot(schedule_version):
    """Capture the mutable inputs used to launch one optimizer run."""
    block = schedule_version.schedule_block
    contract_rows = []
    assignments = apply_effective_contracts(
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
            'facility_settings': contract.facility_settings,
            'workload_settings': contract.workload_settings,
            'shift_settings': contract.shift_settings,
            'night_settings': contract.night_settings,
            'weekend_settings': contract.weekend_settings,
            'request_settings': contract.request_settings,
            'user_rule_overrides': assignment.rule_overrides,
            'user_overrides_updated_at': (
                assignment.overrides_updated_at.isoformat()
                if assignment.overrides_updated_at else None
            ),
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


def _materialize_original_published_snapshot(
    schedule_version, optimizer_run, *, created_by=None,
):
    """Copy the immutable publication snapshot into an isolated new run."""
    snapshot_rows = (schedule_version.original_published_snapshot or {}).get(
        'assignments'
    )
    if snapshot_rows is None:
        raise ValueError('No original published schedule is available.')

    valid_instance_ids = set(
        version_shift_instances_queryset(schedule_version).values_list('id', flat=True)
    )
    physician_ids = {row.get('physician_id') for row in snapshot_rows}
    valid_physician_ids = set(
        Physician.objects.filter(id__in=physician_ids).values_list('id', flat=True)
    )
    if any(
        row.get('shift_instance_id') not in valid_instance_ids
        or row.get('physician_id') not in valid_physician_ids
        for row in snapshot_rows
    ):
        raise ValueError(
            'The original published schedule references a shift or user that no longer exists.'
        )

    ScheduleShiftAssignment.objects.bulk_create([
        ScheduleShiftAssignment(
            shift_instance_id=row['shift_instance_id'],
            physician_id=row['physician_id'],
            created_by=created_by,
            assignment_source=row.get(
                'assignment_source',
                ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            ),
            optimizer_run=optimizer_run,
            is_locked=bool(row.get('is_locked', False)),
        )
        for row in snapshot_rows
    ])


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
    # Runs are pinned before execution. A pre-versioning queued run is
    # explicitly adopted by the frozen stable baseline; unsupported revisions
    # fail closed instead of silently using whatever code is deployed.
    optimizer_run.optimizer_revision = require_supported_v2_revision(
        optimizer_run.optimizer_revision or STABLE_V2_REVISION
    )
    optimizer_run.scoring_revision = (
        optimizer_run.scoring_revision or configured_scoring_revision()
    )
    optimizer_run.optimizer_commit = (
        optimizer_run.optimizer_commit or code_release_identifier()
    )
    fresh_fill = optimizer_run.start_mode == OptimizerRun.StartMode.FRESH_FILL
    original_published = (
        optimizer_run.start_mode
        == OptimizerRun.StartMode.ORIGINAL_PUBLISHED_SCHEDULE
    )
    if not fresh_fill and not original_published:
        if source_run is None or source_run.status != OptimizerRun.Status.COMPLETED:
            raise ValueError('Atlas v2 test requires a completed previous run.')
        if source_run.schedule_version_id != schedule_version.id:
            raise ValueError('Atlas v2 source run belongs to another schedule version.')

    overall_started = monotonic()
    verified_best_score = None

    def publish_verified_best(score, *, force=False):
        """Publish only complete, authoritative, monotonically better scores."""
        nonlocal verified_best_score
        if score is None:
            return False
        candidate = Decimal(str(score))
        if (
            verified_best_score is not None
            and candidate > verified_best_score + Decimal('0.0001')
        ):
            return False
        improved = (
            verified_best_score is None
            or candidate < verified_best_score - Decimal('0.0001')
        )
        if improved:
            verified_best_score = candidate
        if progress_callback is not None and (improved or force):
            progress_callback(verified_best_score, force=force)
        return improved

    bootstrap_summary = None
    if fresh_fill:
        if (
            optimizer_run.optimizer_revision
            == MULTISTART_FRESH_FILL_V2_REVISION
        ):
            bootstrap_summary = _construct_multistart_initial_schedule(
                schedule_version,
                created_by=created_by,
                optimizer_run=optimizer_run,
                stop_requested=stop_requested,
            )
        else:
            bootstrap_summary = construct_complete_initial_schedule(
                schedule_version,
                created_by=created_by,
                optimizer_run=optimizer_run,
                stop_requested=stop_requested,
                # Partial construction scores are not comparable to the score
                # of a complete schedule and therefore are not a verified best.
                progress_callback=None,
            )
        source_run = optimizer_run
    elif original_published:
        _materialize_original_published_snapshot(
            schedule_version,
            optimizer_run,
            created_by=created_by,
        )
        source_run = optimizer_run

    source_report = build_violation_report(
        schedule_version,
        optimizer_run=source_run,
    )
    selected_source_run = source_run
    initial_score = Decimal(str(source_report['total_score']))
    source_initial_score = initial_score
    source_initial_breakdown = source_report['score_breakdown']
    source_completion_summary = None
    if (
        optimizer_run.start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
        and Decimal(str(source_report['score_breakdown'].get('coverage_score', 0)))
        > 0
    ):
        source_completion_summary = complete_initial_schedule_from_source(
            schedule_version,
            optimizer_run=optimizer_run,
            source_run=source_run,
            created_by=created_by,
            stop_requested=stop_requested,
            # Do not publish incomplete-source construction scores as best.
            progress_callback=None,
        )
        source_run = optimizer_run
        source_report = build_violation_report(
            schedule_version,
            optimizer_run=source_run,
        )
        initial_score = Decimal(str(source_report['total_score']))

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
    optimizer_run.configuration_digest = configuration_digest(launch_snapshot)
    source_rescore = {
        'source_run_id': selected_source_run.id,
        'source_run_number': selected_source_run.run_number,
        'source_stored_score': (
            float(selected_source_run.final_score)
            if selected_source_run.final_score is not None else None
        ),
        'current_authoritative_score': float(source_initial_score),
        'current_score_breakdown': source_initial_breakdown,
    }
    optimizer_run.initial_score = source_initial_score
    optimizer_run.score_breakdown = source_initial_breakdown
    optimizer_run.optimizer_debug = {
        'optimizer_engine': 'V2',
        **optimizer_provenance(optimizer_run),
        'launch_snapshot': launch_snapshot,
        'source_rescore': source_rescore,
    }
    optimizer_run.save(update_fields=[
        'initial_score', 'score_breakdown', 'optimizer_debug',
        'optimizer_revision', 'scoring_revision', 'optimizer_commit',
        'configuration_digest',
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
        **optimizer_provenance(optimizer_run),
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
                optimizer_revision=optimizer_run.optimizer_revision,
                distribution_focus=(
                    optimizer_run.optimization_focus
                    == OptimizerRun.OptimizationFocus.DISTRIBUTION
                ),
                stop_requested=stop_requested,
                progress_callback=publish_verified_best,
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
        if (
            verified_best_score is not None
            and final_score > verified_best_score + Decimal('0.0001')
        ):
            raise ValueError(
                'Atlas v2 final penalty exceeded the lowest verified penalty; '
                'the isolated result was not saved.'
            )
        if final_score > source_initial_score + Decimal('0.0001'):
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
            **optimizer_provenance(optimizer_run),
            'optimizer_run_id': optimizer_run.id,
            'optimizer_run_number': optimizer_run.run_number,
            'start_mode': optimizer_run.start_mode,
            'initial_score': float(source_initial_score),
            'final_score': float(final_score),
            'total_score': float(final_score),
            'score_breakdown': report['score_breakdown'],
            'initial_score_breakdown': source_initial_breakdown,
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
            'best_verified_score': float(final_score),
            'completion_score': float(final_score),
            'completion_score_breakdown': report['score_breakdown'],
            'completion_scoring_revision': optimizer_run.scoring_revision,
            'v2_search': _compact_search_summary(search_result),
            'fresh_fill_bootstrap': _compact_bootstrap_summary(
                bootstrap_summary
            ),
            'source_completion_bootstrap': _compact_bootstrap_summary(
                source_completion_summary
            ),
        }
        optimizer_run.status = OptimizerRun.Status.COMPLETED
        optimizer_run.initial_score = source_initial_score
        optimizer_run.final_score = final_score
        optimizer_run.score_breakdown = report['score_breakdown']
        optimizer_run.completion_score = final_score
        optimizer_run.completion_score_breakdown = report['score_breakdown']
        optimizer_run.completion_scoring_revision = optimizer_run.scoring_revision
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
            'completion_score', 'completion_score_breakdown',
            'completion_scoring_revision',
            'optimizer_summary', 'optimizer_debug', 'score_is_stale',
            'is_active', 'notes',
        ])
    publish_verified_best(final_score, force=True)
    return summary


# Existing imports remain valid while callers migrate to the product name.
optimize_schedule_version_v2_test = optimize_schedule_version_v2
