"""Internal cumulative optimizer-lineage measurements."""

from decimal import Decimal


PENALTY_EPSILON = Decimal('0.0001')


def inherited_optimizer_lineage(source_run):
    """Return the persisted lineage carried by an unchanged source schedule."""
    if source_run is None:
        return {
            'lineage_fresh_fill_run_number': None,
            'lineage_cumulative_runtime_seconds': 0.0,
            'lineage_runtime_since_penalty_reduction_seconds': 0.0,
            'lineage_search_exhaustion_count': 0,
        }
    root_run_number = source_run.lineage_fresh_fill_run_number
    if (
        root_run_number is None
        and source_run.start_mode == 'FRESH_FILL'
    ):
        root_run_number = source_run.run_number
    return {
        'lineage_fresh_fill_run_number': root_run_number,
        'lineage_cumulative_runtime_seconds': max(
            float(source_run.lineage_cumulative_runtime_seconds or 0.0), 0.0,
        ),
        'lineage_runtime_since_penalty_reduction_seconds': max(
            float(
                source_run.lineage_runtime_since_penalty_reduction_seconds
                or 0.0
            ),
            0.0,
        ),
        'lineage_search_exhaustion_count': max(
            int(source_run.lineage_search_exhaustion_count or 0), 0,
        ),
    }


def _best_verified_elapsed_seconds(search_result):
    checkpoint = search_result.get('best_authoritative_checkpoint') or {}
    elapsed = checkpoint.get('elapsed_seconds')
    if elapsed is None:
        telemetry = search_result.get('production_telemetry') or {}
        elapsed = telemetry.get('best_verified_seconds')
    return None if elapsed is None else max(float(elapsed), 0.0)


def completed_optimizer_lineage(
    optimizer_run,
    *,
    runtime_seconds,
    initial_score,
    final_score,
    stopped_reason,
    search_result,
    search_started_elapsed_seconds,
):
    """Calculate the lineage metadata for one completed optimizer result."""
    source_values = inherited_optimizer_lineage(
        optimizer_run.started_from_run,
    )
    runtime_seconds = max(float(runtime_seconds or 0.0), 0.0)
    root_run_number = source_values['lineage_fresh_fill_run_number']
    if optimizer_run.start_mode == 'FRESH_FILL':
        root_run_number = optimizer_run.run_number

    initial = Decimal(str(initial_score))
    final = Decimal(str(final_score))
    penalty_reduced = final < initial - PENALTY_EPSILON
    inherited_stagnation = source_values[
        'lineage_runtime_since_penalty_reduction_seconds'
    ]

    if penalty_reduced:
        best_elapsed = _best_verified_elapsed_seconds(search_result)
        if best_elapsed is None or search_started_elapsed_seconds is None:
            runtime_since_reduction = 0.0
        else:
            last_reduction_elapsed = (
                max(float(search_started_elapsed_seconds), 0.0)
                + best_elapsed
            )
            runtime_since_reduction = max(
                runtime_seconds - last_reduction_elapsed, 0.0,
            )
    elif optimizer_run.optimization_focus == 'DISTRIBUTION':
        # Distribution work may be productive without lowering the official
        # penalty, so it must not age the Standard-focus stagnation clock.
        runtime_since_reduction = inherited_stagnation
    else:
        runtime_since_reduction = inherited_stagnation + runtime_seconds

    return {
        'lineage_fresh_fill_run_number': root_run_number,
        'lineage_cumulative_runtime_seconds': (
            source_values['lineage_cumulative_runtime_seconds']
            + runtime_seconds
        ),
        'lineage_runtime_since_penalty_reduction_seconds': (
            runtime_since_reduction
        ),
        'lineage_search_exhaustion_count': (
            source_values['lineage_search_exhaustion_count']
            + int(stopped_reason == 'productivity_exhausted')
        ),
    }
