"""Engine-neutral entry point for constructing a complete initial schedule.

The implementation deliberately delegates to Atlas's proven constructor for
now.  Keeping this compatibility boundary small lets V2 become the primary
optimizer without also rewriting the safety-critical Fresh Fill behavior.
"""

from .models import OptimizerRun
from .optimizer import optimize_schedule_version


def construct_complete_initial_schedule(
    schedule_version,
    *,
    optimizer_run,
    created_by=None,
    stop_requested=None,
    progress_callback=None,
):
    """Construct and persist the first complete, hard-legal Fresh Fill.

    This is construction only.  The callback stops the proven optimizer as
    soon as its first complete schedule is available, before its improvement
    portfolio begins.  The returned schedule remains isolated on the supplied
    optimizer run for the selected optimization engine to improve.
    """
    def construction_progress(score, force=False):
        if progress_callback is not None:
            progress_callback(score, force=force)

    summary = optimize_schedule_version(
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
        progress_callback=construction_progress,
        isolated_run=True,
        finalize_run=False,
        construction_only=True,
    )
    if int(summary.get('unfilled_shift_count') or 0) > 0:
        raise ValueError(
            'Fresh Fill could not construct a complete starting schedule '
            'within the selected runtime.'
        )
    if int(summary.get('final_overlap_violations') or 0) > 0:
        raise ValueError(
            'Fresh Fill produced a time-overlap conflict during construction; '
            'no result was saved.'
        )
    optimizer_run.refresh_from_db()
    return summary
