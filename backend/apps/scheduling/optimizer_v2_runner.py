"""Isolated persistence wrapper for the opt-in Atlas v2 test engine."""

import io
import json
from decimal import Decimal
from time import monotonic

from django.core.management import call_command
from django.db import transaction

from .models import OptimizerRun, ScheduleShiftAssignment
from .optimizer import (
    _state_from_assignments,
    _version_shift_instances_queryset,
    build_violation_report,
    optimize_schedule_version,
)
from .optimizer_v2 import (
    reassign_assignment,
    rotate_assignments,
    swap_assignments,
)
from .run_state import assignments_for_viewed_run


V2_TEST_RUN_KIND = 'OPTIMIZER_V2_TEST'


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


def optimize_schedule_version_v2_test(
    schedule_version,
    *,
    optimizer_run,
    source_run,
    created_by=None,
    stop_requested=None,
    progress_callback=None,
):
    """Build or copy an isolated start, run v2, and save one result."""
    if optimizer_run.run_kind != V2_TEST_RUN_KIND:
        raise ValueError('Atlas v2 test requires an Atlas v2 test run record.')
    fresh_fill = optimizer_run.start_mode == OptimizerRun.StartMode.FRESH_FILL
    if not fresh_fill:
        if source_run is None or source_run.status != OptimizerRun.Status.COMPLETED:
            raise ValueError('Atlas v2 test requires a completed previous run.')
        if source_run.schedule_version_id != schedule_version.id:
            raise ValueError('Atlas v2 source run belongs to another schedule version.')

    overall_started = monotonic()
    bootstrap_summary = None
    if fresh_fill:
        complete_schedule_seen = [False]

        def bootstrap_progress(score, force=False):
            complete_schedule_seen[0] = True
            if progress_callback is not None:
                progress_callback(score, force=force)

        def bootstrap_stop_requested():
            return complete_schedule_seen[0] or bool(
                stop_requested is not None and stop_requested()
            )

        bootstrap_summary = optimize_schedule_version(
            schedule_version,
            created_by=created_by,
            optimizer_run=optimizer_run,
            seed=optimizer_run.seed,
            start_mode=OptimizerRun.StartMode.FRESH_FILL,
            source_run=source_run,
            max_runtime_seconds=optimizer_run.max_runtime_seconds,
            optimization_focus=optimizer_run.optimization_focus,
            adaptive_runtime=True,
            stop_requested=bootstrap_stop_requested,
            progress_callback=bootstrap_progress,
            isolated_run=True,
            finalize_run=False,
        )
        if int(bootstrap_summary.get('unfilled_shift_count') or 0) > 0:
            raise ValueError(
                'Atlas v2 Fresh Fill could not construct a complete starting '
                'schedule within the selected runtime.'
            )
        if int(bootstrap_summary.get('final_overlap_violations') or 0) > 0:
            raise ValueError(
                'Atlas v2 Fresh Fill produced a time-overlap conflict during '
                'construction; no result was saved.'
            )
        optimizer_run.refresh_from_db()
        source_run = optimizer_run

    source_report = build_violation_report(
        schedule_version,
        optimizer_run=source_run,
    )
    initial_score = Decimal(str(source_report['total_score']))
    if progress_callback is not None:
        progress_callback(initial_score, force=True)

    output = io.StringIO()
    remaining_runtime_seconds = max(
        float(optimizer_run.max_runtime_seconds) - (monotonic() - overall_started),
        0.0,
    )
    user_stopped = bool(stop_requested is not None and stop_requested())
    if remaining_runtime_seconds <= 0 or user_stopped:
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
            # The selected transition is always checked authoritatively by the
            # kernel. Additional sampled candidates are a diagnostic benchmark,
            # not production work.
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
            as_json=True,
            stdout=output,
        )
        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        if not lines:
            raise ValueError('Atlas v2 test did not return a search result.')
        search_result = json.loads(lines[-1])

    source_assignments = list(
        assignments_for_viewed_run(schedule_version, source_run)
        .select_related('shift_instance', 'physician')
        .order_by('shift_instance_id', 'physician_id', 'id')
    )
    state, _locked_manual_pairs = _state_from_assignments(source_assignments)
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
        if Decimal(str(report['score_breakdown']['overlap_score'])) > 0:
            raise ValueError(
                'Atlas v2 produced a time-overlap conflict; the isolated '
                'result was not saved.'
            )
        if final_score > initial_score + Decimal('0.0001'):
            raise ValueError(
                'Atlas v2 test result worsened the authoritative penalty; '
                'the isolated result was not saved.'
            )
        instance_required = dict(
            _version_shift_instances_queryset(schedule_version).values_list(
                'id', 'required_staffing',
            )
        )
        unfilled = sum(
            max(int(required) - len(state.get(instance_id, ())), 0)
            for instance_id, required in instance_required.items()
        )
        summary = {
            'message': 'Atlas v2 test run completed.',
            'optimizer_engine': 'V2_TEST',
            'optimizer_run_id': optimizer_run.id,
            'optimizer_run_number': optimizer_run.run_number,
            'start_mode': optimizer_run.start_mode,
            'initial_score': float(initial_score),
            'final_score': float(final_score),
            'total_score': float(final_score),
            'score_breakdown': report['score_breakdown'],
            'initial_score_breakdown': source_report['score_breakdown'],
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
            'v2_search': search_result,
            'fresh_fill_bootstrap': bootstrap_summary,
        }
        optimizer_run.status = OptimizerRun.Status.COMPLETED
        optimizer_run.initial_score = initial_score
        optimizer_run.final_score = final_score
        optimizer_run.score_breakdown = report['score_breakdown']
        optimizer_run.optimizer_summary = summary
        optimizer_run.optimizer_debug = {
            'optimizer_engine': 'V2_TEST',
            'search': search_result,
            'score_audit': report.get('score_audit'),
        }
        optimizer_run.score_is_stale = False
        optimizer_run.is_active = False
        optimizer_run.notes = (
            'Atlas v2 Fresh Fill result; historical and active schedules were preserved.'
            if fresh_fill
            else 'Atlas v2 test result; source and active schedules were preserved.'
        )
        optimizer_run.save(update_fields=[
            'status', 'initial_score', 'final_score', 'score_breakdown',
            'optimizer_summary', 'optimizer_debug', 'score_is_stale',
            'is_active', 'notes',
        ])
    if progress_callback is not None:
        progress_callback(final_score, force=True)
    return summary
