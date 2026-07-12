"""Canonical Schedule Build Workspace optimizer-run state rules.

This module names the four run roles used by the workspace.  It deliberately
does not add persistence: viewed_run is request state, active_run is persisted
by OptimizerRun.is_active, source_run is an optimizer/copy input, and
created_run is the result of that operation.
"""

from dataclasses import dataclass

from django.db.models import Q

from .models import OptimizerRun, ScheduleShiftAssignment


@dataclass(frozen=True)
class BuildWorkspaceRunContext:
    viewed_run: OptimizerRun | None
    active_run: OptimizerRun | None

    @property
    def viewed_run_is_editable(self):
        return bool(
            self.viewed_run
            and self.viewed_run.status == OptimizerRun.Status.COMPLETED
            and self.viewed_run.id == getattr(self.active_run, 'id', None)
        )

    @property
    def viewed_run_can_activate(self):
        return bool(
            self.viewed_run
            and self.viewed_run.status == OptimizerRun.Status.COMPLETED
            and not self.viewed_run_is_editable
        )

    @property
    def viewed_run_can_copy(self):
        return bool(self.viewed_run and self.viewed_run.status == OptimizerRun.Status.COMPLETED)

    @property
    def viewed_run_can_be_optimizer_source(self):
        return self.viewed_run_can_copy


def get_active_optimizer_run(version):
    return version.optimizer_runs.filter(
        is_active=True, status=OptimizerRun.Status.COMPLETED,
    ).order_by('-run_number').first()


def get_viewed_optimizer_run(version, requested_run_id=None):
    """Resolve URL selection, then active, then newest completed.

    Failed/running/timed-out runs remain in history but are not displayable as
    schedule state. Invalid, cross-version, and non-completed IDs use the same
    safe fallback. Old rows with missing/defaulted start_mode need no special
    selection branch.
    """
    active_run = get_active_optimizer_run(version)
    fallback = active_run or version.optimizer_runs.filter(
        status=OptimizerRun.Status.COMPLETED,
    ).order_by('-run_number').first()
    if requested_run_id in (None, ''):
        return fallback
    try:
        requested_run_id = int(requested_run_id)
    except (TypeError, ValueError):
        return fallback
    return version.optimizer_runs.filter(
        id=requested_run_id, status=OptimizerRun.Status.COMPLETED,
    ).first() or fallback


def resolve_build_workspace_run_context(version, requested_run_id=None):
    return BuildWorkspaceRunContext(
        viewed_run=get_viewed_optimizer_run(version, requested_run_id),
        active_run=get_active_optimizer_run(version),
    )


def visible_assignment_filter(viewed_run):
    if viewed_run is not None and viewed_run.run_kind == 'COPY':
        return Q(optimizer_run=viewed_run)
    query = Q(
        assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
        optimizer_run__isnull=True,
    )
    if viewed_run is not None:
        query |= Q(optimizer_run=viewed_run)
    return query


def locked_open_ids(viewed_run):
    return set(viewed_run.locked_open_shift_instance_ids or []) if viewed_run else set()


def serialize_run_state(context):
    return {
        'viewed_run_id': context.viewed_run.id if context.viewed_run else None,
        'active_run_id': context.active_run.id if context.active_run else None,
        'viewed_run_is_editable': context.viewed_run_is_editable,
        'viewed_run_can_activate': context.viewed_run_can_activate,
        'viewed_run_can_copy': context.viewed_run_can_copy,
        'viewed_run_can_be_optimizer_source': context.viewed_run_can_be_optimizer_source,
    }
