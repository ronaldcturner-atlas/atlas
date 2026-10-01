"""Canonical persisted-assignment snapshots shared by optimizer engines."""

from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Count

from .models import (
    ContractUserAssignment,
    OptimizerRun,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
)
from .run_state import assignments_for_viewed_run


@dataclass
class PreparedAssignmentSnapshot:
    assignment_rows_before: int
    optimizer_assignments_deleted: int
    instances: list
    source_assignments: list
    source_assignment_normalization: dict
    manual_assignment_only_physician_ids: set[int]
    manual_only_optimizer_source_rows_dropped: int
    source_assignment_count_raw: int
    source_assignment_count: int
    assignments: list
    manual_assignments_preserved: int
    duplicate_shift_instances: list
    state: defaultdict
    manual_pairs: set[tuple[int, int]]
    runless_manual_overlay_pairs: set[tuple[int, int]]
    source_visible_assignment_pairs: set[tuple[int, int]]
    loaded_start_assignment_pairs: set[tuple[int, int]]
    assignments_same_at_start: bool
    source_pairs_missing_at_start: list
    source_pairs_extra_at_start: list
    instances_by_id: dict


@dataclass
class PreparedRequestSnapshot:
    requests_by_physician_date: defaultdict
    manual_only_request_assignment_rows: list
    manual_only_request_assignments_seeded: int
    manual_only_request_assignments_already_present: int
    manual_only_request_optimizer_owners_displaced: int
    manual_only_request_conflicts: list


def version_shift_instances_queryset(version):
    return ScheduleShiftInstance.objects.filter(
        schedule_version=version,
        date__gte=version.schedule_block.start_date,
        date__lte=version.schedule_block.end_date,
    )


def state_from_assignments(assignments):
    state = defaultdict(list)
    manual_pairs = set()
    for assignment in assignments:
        if assignment.physician_id not in state[assignment.shift_instance_id]:
            state[assignment.shift_instance_id].append(assignment.physician_id)
        if (
            assignment.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
        ):
            manual_pairs.add(
                (assignment.shift_instance_id, assignment.physician_id)
            )
    return state, manual_pairs


def canonical_assignment_snapshot(
    assignments, instances, selected_run=None, preserve_physician_ids=None,
):
    """Deduplicate and cap an assignment snapshot without mutating source rows."""
    preserve_physician_ids = set(preserve_physician_ids or ())
    required_by_instance = {
        instance.id: instance.required_staffing for instance in instances
    }
    selected_run_id = getattr(selected_run, 'id', None)

    def priority(assignment):
        locked_manual = (
            assignment.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
        )
        return (
            0 if locked_manual else 1,
            0 if assignment.optimizer_run_id == selected_run_id else 1,
            0 if assignment.optimizer_run_id is not None else 1,
            assignment.id or 0,
        )

    kept = []
    seen_pairs = set()
    counts_by_instance = defaultdict(int)
    duplicate_rows = []
    excess_rows = []
    for assignment in sorted(assignments, key=priority):
        pair = (assignment.shift_instance_id, assignment.physician_id)
        if pair in seen_pairs:
            duplicate_rows.append(assignment)
            continue
        required = required_by_instance.get(assignment.shift_instance_id)
        if (
            required is None
            or (
                counts_by_instance[assignment.shift_instance_id] >= required
                and assignment.physician_id not in preserve_physician_ids
            )
        ):
            excess_rows.append(assignment)
            continue
        seen_pairs.add(pair)
        counts_by_instance[assignment.shift_instance_id] += 1
        kept.append(assignment)
    return kept, {
        'duplicate_rows_discarded': len(duplicate_rows),
        'excess_rows_discarded': len(excess_rows),
        'discarded_assignment_ids': [
            assignment.id for assignment in [*duplicate_rows, *excess_rows]
            if assignment.id is not None
        ],
    }


def prepare_assignment_snapshot(
    version,
    *,
    source_run,
    optimizer_run,
    start_mode,
    source_locked_open_ids,
    isolated_run,
    created_by,
):
    """Load and normalize the authoritative assignment starting snapshot."""
    assignment_rows_before = ScheduleShiftAssignment.objects.filter(
        shift_instance__schedule_version=version,
        shift_instance__date__gte=version.schedule_block.start_date,
        shift_instance__date__lte=version.schedule_block.end_date,
    ).count()
    optimizer_assignments_deleted = 0
    if not isolated_run:
        ScheduleShiftInstance.objects.filter(schedule_version=version).update(
            is_locked_open=False,
        )
        ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            id__in=source_locked_open_ids,
        ).update(is_locked_open=True)
    instances_queryset = version_shift_instances_queryset(version)
    if not isolated_run:
        instances_queryset = instances_queryset.select_for_update()
    instances = list(
        instances_queryset
        .select_related('facility', 'shift_template')
        .order_by('date', 'facility__name', 'start_datetime', 'id')
    )
    if isolated_run:
        source_locked_open_id_set = set(source_locked_open_ids)
        for instance in instances:
            instance.is_locked_open = instance.id in source_locked_open_id_set
    raw_source_assignments = list(
        assignments_for_viewed_run(version, source_run)
        .select_related('shift_instance', 'physician__user')
    )
    manual_assignment_only_physician_ids = set(
        ContractUserAssignment.objects.filter(
            domain=version.domain,
            contract__active=True,
            contract__manual_assignment_only=True,
            physician__active=True,
        ).values_list('physician_id', flat=True)
    )
    source_assignments, source_assignment_normalization = (
        canonical_assignment_snapshot(
            raw_source_assignments,
            instances,
            selected_run=source_run,
            preserve_physician_ids=manual_assignment_only_physician_ids,
        )
    )
    # A physician can be switched to a manual-only contract after an older
    # optimizer run assigned them shifts. Those optimizer-owned rows must not
    # become frozen placeholder assignments in every later run.
    manual_only_optimizer_source_rows_dropped = sum(
        1
        for row in source_assignments
        if (
            row.physician_id in manual_assignment_only_physician_ids
            and row.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
        )
    )
    source_assignments = [
        row
        for row in source_assignments
        if not (
            row.physician_id in manual_assignment_only_physician_ids
            and row.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
        )
    ]
    source_assignment_count_raw = len(raw_source_assignments)

    if source_assignments:
        source_assignment_count = len(source_assignments)
        # Self-contained runs cannot rely on the runless manual overlay.
        if (
            source_run is None
            and optimizer_run.run_kind not in ('COPY', 'BENCHMARK')
        ):
            assignments = [
                row for row in source_assignments
                if (
                    start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    or row.is_locked
                    or row.physician_id
                    in manual_assignment_only_physician_ids
                )
            ]
        else:
            manual_seed_rows_by_pair = {}
            manual_overlay_rows = []
            for row in source_assignments:
                if (
                    row.assignment_source
                    != ScheduleShiftAssignment.AssignmentSource.MANUAL
                    or not (
                        start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                        or row.is_locked
                    )
                ):
                    continue
                if (
                    row.optimizer_run_id is None
                    and optimizer_run.run_kind not in ('COPY', 'BENCHMARK')
                ):
                    manual_overlay_rows.append(row)
                    continue
                pair = (row.shift_instance_id, row.physician_id)
                manual_seed_rows_by_pair[pair] = ScheduleShiftAssignment(
                    shift_instance_id=row.shift_instance_id,
                    physician_id=row.physician_id,
                    created_by=created_by,
                    assignment_source=(
                        ScheduleShiftAssignment.AssignmentSource.MANUAL
                    ),
                    optimizer_run=optimizer_run,
                    is_locked=row.is_locked,
                )
            ScheduleShiftAssignment.objects.bulk_create(
                list(manual_seed_rows_by_pair.values())
            )
            assignments = (
                [
                    row for row in source_assignments
                    if (
                        row.assignment_source
                        == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
                        and (
                            start_mode
                            == OptimizerRun.StartMode.CURRENT_SCHEDULE
                            or row.physician_id
                            in manual_assignment_only_physician_ids
                        )
                    )
                ]
                + manual_overlay_rows
                + list(
                    ScheduleShiftAssignment.objects.filter(
                        optimizer_run=optimizer_run,
                        assignment_source=(
                            ScheduleShiftAssignment.AssignmentSource.MANUAL
                        ),
                    ).select_related('shift_instance', 'physician__user')
                )
            )
    else:
        source_assignment_count = 0
        assignments = []

    manual_assignments_preserved = sum(
        1
        for assignment in assignments
        if (
            assignment.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.is_locked
        )
    )
    duplicate_shift_instances = list(
        version_shift_instances_queryset(version)
        .values('date', 'shift_template_id')
        .annotate(row_count=Count('id'))
        .filter(row_count__gt=1)
    )
    state, manual_pairs = state_from_assignments(assignments)
    runless_manual_overlay_pairs = {
        (assignment.shift_instance_id, assignment.physician_id)
        for assignment in assignments
        if (
            assignment.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.MANUAL
            and assignment.optimizer_run_id is None
        )
    }
    manual_pairs.update(
        (instance_id, physician_id)
        for instance_id, physician_ids in state.items()
        for physician_id in physician_ids
        if physician_id in manual_assignment_only_physician_ids
    )
    source_visible_assignment_pairs = {
        (assignment.shift_instance_id, assignment.physician_id)
        for assignment in source_assignments
    }
    loaded_start_assignment_pairs = {
        (instance_id, physician_id)
        for instance_id, physician_ids in state.items()
        for physician_id in physician_ids
    }

    return PreparedAssignmentSnapshot(
        assignment_rows_before=assignment_rows_before,
        optimizer_assignments_deleted=optimizer_assignments_deleted,
        instances=instances,
        source_assignments=source_assignments,
        source_assignment_normalization=source_assignment_normalization,
        manual_assignment_only_physician_ids=(
            manual_assignment_only_physician_ids
        ),
        manual_only_optimizer_source_rows_dropped=(
            manual_only_optimizer_source_rows_dropped
        ),
        source_assignment_count_raw=source_assignment_count_raw,
        source_assignment_count=source_assignment_count,
        assignments=assignments,
        manual_assignments_preserved=manual_assignments_preserved,
        duplicate_shift_instances=duplicate_shift_instances,
        state=state,
        manual_pairs=manual_pairs,
        runless_manual_overlay_pairs=runless_manual_overlay_pairs,
        source_visible_assignment_pairs=source_visible_assignment_pairs,
        loaded_start_assignment_pairs=loaded_start_assignment_pairs,
        assignments_same_at_start=(
            loaded_start_assignment_pairs == source_visible_assignment_pairs
        ),
        source_pairs_missing_at_start=sorted(
            source_visible_assignment_pairs - loaded_start_assignment_pairs
        ),
        source_pairs_extra_at_start=sorted(
            loaded_start_assignment_pairs - source_visible_assignment_pairs
        ),
        instances_by_id={instance.id: instance for instance in instances},
    )


def prepare_authoritative_requests(
    version,
    *,
    instances,
    state,
    manual_pairs,
    assignments,
    manual_assignment_only_physician_ids,
    optimizer_run,
    created_by,
):
    """Load requests and seed authoritative manual-only Shift On rows."""
    requests = (
        ScheduleRequest.objects.filter(
            schedule_block=version.schedule_block,
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .prefetch_related('shift_templates')
    )
    requests_by_physician_date = defaultdict(list)
    for schedule_request in requests:
        requests_by_physician_date[
            (schedule_request.physician_id, schedule_request.date)
        ].append(schedule_request)

    # For a manual-only physician, Shift On is the scheduler's authoritative
    # assignment instruction regardless of scope or weight. Only replaceable
    # optimizer occupants may be displaced.
    instances_by_date_template = defaultdict(list)
    for instance in instances:
        instances_by_date_template[
            (instance.date, instance.shift_template_id)
        ].append(instance)
    manual_only_request_assignment_rows = []
    manual_only_request_assignments_seeded = 0
    manual_only_request_assignments_already_present = 0
    manual_only_request_optimizer_owners_displaced = 0
    manual_only_request_conflicts = []
    for (
        physician_id,
        request_date,
    ), schedule_requests in requests_by_physician_date.items():
        if physician_id not in manual_assignment_only_physician_ids:
            continue
        for schedule_request in schedule_requests:
            if schedule_request.request_type != ScheduleRequest.RequestType.SHIFT_ON:
                continue
            matching_instances = []
            for template in schedule_request.shift_templates.all():
                matching_instances.extend(
                    instances_by_date_template.get(
                        (request_date, template.id),
                        (),
                    )
                )
            matching_instances.sort(
                key=lambda item: (
                    item.start_datetime,
                    item.end_datetime,
                    item.id,
                )
            )
            if not matching_instances:
                manual_only_request_conflicts.append({
                    'request_id': schedule_request.id,
                    'physician_id': physician_id,
                    'date': request_date.isoformat(),
                    'reason': 'no_matching_shift_instance',
                })
                continue
            instance = matching_instances[0]
            pair = (instance.id, physician_id)
            if physician_id in state[instance.id]:
                manual_pairs.add(pair)
                manual_only_request_assignments_already_present += 1
                continue

            replaceable_owner_ids = [
                owner_id
                for owner_id in state[instance.id]
                if (instance.id, owner_id) not in manual_pairs
            ]
            while (
                len(state[instance.id]) >= instance.required_staffing
                and replaceable_owner_ids
            ):
                owner_id = replaceable_owner_ids.pop()
                state[instance.id].remove(owner_id)
                manual_only_request_optimizer_owners_displaced += 1
            if len(state[instance.id]) >= instance.required_staffing:
                manual_only_request_conflicts.append({
                    'request_id': schedule_request.id,
                    'physician_id': physician_id,
                    'date': request_date.isoformat(),
                    'shift_instance_id': instance.id,
                    'reason': 'conflicts_with_existing_fixed_assignment',
                })

            state[instance.id].append(physician_id)
            manual_pairs.add(pair)
            manual_only_request_assignment_rows.append(
                ScheduleShiftAssignment(
                    shift_instance=instance,
                    physician_id=physician_id,
                    created_by=created_by,
                    assignment_source=(
                        ScheduleShiftAssignment.AssignmentSource.MANUAL
                    ),
                    optimizer_run=optimizer_run,
                    is_locked=True,
                )
            )
            manual_only_request_assignments_seeded += 1
    if manual_only_request_assignment_rows:
        ScheduleShiftAssignment.objects.bulk_create(
            manual_only_request_assignment_rows,
            batch_size=500,
        )
        assignments.extend(manual_only_request_assignment_rows)

    return PreparedRequestSnapshot(
        requests_by_physician_date=requests_by_physician_date,
        manual_only_request_assignment_rows=manual_only_request_assignment_rows,
        manual_only_request_assignments_seeded=(
            manual_only_request_assignments_seeded
        ),
        manual_only_request_assignments_already_present=(
            manual_only_request_assignments_already_present
        ),
        manual_only_request_optimizer_owners_displaced=(
            manual_only_request_optimizer_owners_displaced
        ),
        manual_only_request_conflicts=manual_only_request_conflicts,
    )
