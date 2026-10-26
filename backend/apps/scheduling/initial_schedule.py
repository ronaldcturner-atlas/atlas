"""Engine-neutral entry point for constructing a complete initial schedule.

The implementation deliberately delegates to Atlas's proven constructor for
now. Keeping this compatibility boundary small lets V2 remain the primary
improvement engine without also rewriting safety-critical Fresh Fill tactics.
"""

from .optimizer_v1_constructor import (
    complete_schedule_from_source,
    construct_complete_fresh_fill_schedule,
)


def construct_complete_initial_schedule(
    schedule_version,
    *,
    optimizer_run,
    seed=None,
    created_by=None,
    stop_requested=None,
    progress_callback=None,
):
    """Construct and persist the first complete, hard-legal Fresh Fill.

    The returned schedule remains isolated on the supplied optimizer run for
    the selected improvement engine. The V1 compatibility adapter owns the
    internal switches that stop after the first complete, valid schedule.
    """
    def construction_progress(score, force=False):
        if progress_callback is not None:
            progress_callback(score, force=force)

    constructor_options = {
        'created_by': created_by,
        'optimizer_run': optimizer_run,
        'stop_requested': stop_requested,
        'progress_callback': construction_progress,
    }
    if seed is not None:
        constructor_options['seed'] = seed
    summary = construct_complete_fresh_fill_schedule(
        schedule_version,
        **constructor_options,
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


def complete_initial_schedule_from_source(
    schedule_version,
    *,
    optimizer_run,
    source_run,
    created_by=None,
    stop_requested=None,
    progress_callback=None,
):
    """Fill current, newly open slots before handing the schedule to V2."""
    summary = complete_schedule_from_source(
        schedule_version,
        optimizer_run=optimizer_run,
        source_run=source_run,
        created_by=created_by,
        stop_requested=stop_requested,
        progress_callback=progress_callback,
    )
    if int(summary.get('unfilled_shift_count') or 0) > 0:
        raise ValueError(
            'The selected schedule could not fill all newly open shifts '
            'within the selected runtime.'
        )
    if int(summary.get('final_overlap_violations') or 0) > 0:
        raise ValueError(
            'Filling newly open shifts produced a time-overlap conflict; '
            'no result was saved.'
        )
    optimizer_run.refresh_from_db()
    return summary
