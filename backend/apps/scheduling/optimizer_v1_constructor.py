"""Compatibility provider for Atlas's proven Fresh Fill placement pipeline."""

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
