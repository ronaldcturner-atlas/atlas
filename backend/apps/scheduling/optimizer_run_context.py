"""Run-lineage and immutable-start preparation for optimizer execution."""

from dataclasses import dataclass
import secrets

from .models import OptimizerRun, ScheduleShiftInstance, ScheduleVersion


@dataclass(frozen=True)
class PreparedOptimizerRun:
    version: ScheduleVersion
    source_run: OptimizerRun | None
    optimizer_run: OptimizerRun
    source_locked_open_ids: list[int]
    runtime_limit_seconds: int
    optimization_focus: str
    seed: int


def prepare_optimizer_run(
    schedule_version,
    *,
    created_by,
    optimizer_run,
    seed,
    start_mode,
    source_run,
    run_kind,
    isolated_run,
    runtime_limit_seconds,
    max_runtime_seconds,
    optimization_focus,
):
    """Resolve one optimizer run and its explicit starting lineage.

    The caller owns the surrounding database transaction. Keeping this policy
    in one place prevents construction and improvement engines from silently
    choosing different source runs, seeds, locked-open snapshots, or runtimes.
    """
    version_queryset = ScheduleVersion.objects.select_related(
        'schedule_block', 'domain',
    )
    if not isolated_run:
        version_queryset = version_queryset.select_for_update()
    version = version_queryset.get(id=schedule_version.id)
    if start_mode not in OptimizerRun.StartMode.values:
        raise ValueError('Invalid optimizer start mode.')
    if source_run is not None:
        source_run_queryset = OptimizerRun.objects
        if not isolated_run:
            source_run_queryset = source_run_queryset.select_for_update()
        source_run = source_run_queryset.get(
            id=source_run.id,
            schedule_version=version,
            status=OptimizerRun.Status.COMPLETED,
        )
    source_locked_open_ids = (
        list(source_run.locked_open_shift_instance_ids or [])
        if source_run is not None
        else list(
            ScheduleShiftInstance.objects.filter(
                schedule_version=version, is_locked_open=True,
            ).values_list('id', flat=True)
        )
    )
    if optimizer_run is None:
        latest_run_number = (
            OptimizerRun.objects.filter(schedule_version=version)
            .order_by('-run_number')
            .values_list('run_number', flat=True)
            .first()
            or 0
        )
        if seed is None:
            seed = secrets.randbits(63)
        optimizer_run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=latest_run_number + 1,
            created_by=created_by,
            status=OptimizerRun.Status.RUNNING,
            seed=seed,
            start_mode=start_mode,
            started_from_run=(
                source_run
                if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                else None
            ),
            started_from_run_number=(
                source_run.run_number
                if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                and source_run is not None
                else None
            ),
            initial_score=(
                source_run.final_score
                if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                and source_run is not None
                else None
            ),
            max_runtime_seconds=runtime_limit_seconds,
            optimization_focus=optimization_focus,
            run_kind=run_kind,
            locked_open_shift_instance_ids=source_locked_open_ids,
        )
    else:
        optimizer_run_queryset = OptimizerRun.objects
        if not isolated_run:
            optimizer_run_queryset = optimizer_run_queryset.select_for_update()
        optimizer_run = optimizer_run_queryset.get(
            id=optimizer_run.id,
            schedule_version=version,
        )
        expected_started_from_run = (
            source_run
            if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
            else None
        )
        expected_started_from_number = (
            source_run.run_number
            if expected_started_from_run is not None
            else None
        )
        lineage_updates = []
        if optimizer_run.started_from_run_id != (
            expected_started_from_run.id if expected_started_from_run else None
        ):
            optimizer_run.started_from_run = expected_started_from_run
            lineage_updates.append('started_from_run')
        if optimizer_run.started_from_run_number != expected_started_from_number:
            optimizer_run.started_from_run_number = expected_started_from_number
            lineage_updates.append('started_from_run_number')
        if (
            expected_started_from_run is not None
            and optimizer_run.initial_score != expected_started_from_run.final_score
        ):
            optimizer_run.initial_score = expected_started_from_run.final_score
            lineage_updates.append('initial_score')
        if lineage_updates:
            optimizer_run.save(update_fields=lineage_updates)
        runtime_limit_seconds = int(
            max_runtime_seconds
            if max_runtime_seconds is not None
            else optimizer_run.max_runtime_seconds
        )
        if not 0 <= runtime_limit_seconds <= 4 * 60 * 60:
            raise ValueError('Maximum optimizer runtime cannot exceed 240 minutes.')
        if optimizer_run.max_runtime_seconds != runtime_limit_seconds:
            optimizer_run.max_runtime_seconds = runtime_limit_seconds
            optimizer_run.save(update_fields=['max_runtime_seconds'])
        optimization_focus = optimizer_run.optimization_focus
        if seed is not None and optimizer_run.seed != seed:
            optimizer_run.seed = seed
            optimizer_run.save(update_fields=['seed'])
    if optimizer_run.seed is None:
        optimizer_run.seed = seed if seed is not None else secrets.randbits(63)
        optimizer_run.save(update_fields=['seed'])

    return PreparedOptimizerRun(
        version=version,
        source_run=source_run,
        optimizer_run=optimizer_run,
        source_locked_open_ids=source_locked_open_ids,
        runtime_limit_seconds=runtime_limit_seconds,
        optimization_focus=optimization_focus,
        seed=optimizer_run.seed,
    )
