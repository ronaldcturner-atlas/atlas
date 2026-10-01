"""Reusable building blocks for the Atlas v2 high-throughput optimizer.

The v2 engine is intentionally isolated from the production v1 optimizer while
its compiled candidate-generation and incremental-refresh path is integrated.
"""

from dataclasses import dataclass
from types import MappingProxyType
from hashlib import blake2b

import numpy as np


def published_boundary_overlap_matrix(
    physician_ids,
    shift_starts,
    shift_ends,
    boundary_context,
):
    """Return hard conflicts between candidate shifts and published context.

    Endpoints that merely touch are not overlaps. Any positive clock-time
    intersection is a hard conflict and cannot be traded for a lower score.
    """
    starts = np.asarray(shift_starts, dtype=np.float64)
    ends = np.asarray(shift_ends, dtype=np.float64)
    result = np.zeros((len(physician_ids), len(starts)), dtype=np.bool_)
    for physician_index, physician_id in enumerate(physician_ids):
        prior = tuple(boundary_context.get(int(physician_id), ()))
        if not prior:
            continue
        prior_starts = np.asarray(
            [row.start_datetime.timestamp() for row in prior],
            dtype=np.float64,
        )
        prior_ends = np.asarray(
            [row.end_datetime.timestamp() for row in prior],
            dtype=np.float64,
        )
        result[physician_index] = np.any(
            (starts[:, None] < prior_ends[None, :])
            & (ends[:, None] > prior_starts[None, :]),
            axis=1,
        )
    return result


def published_boundary_conflict_matrix(
    physician_ids,
    shift_starts,
    shift_ends,
    boundary_context,
    minimum_rest_seconds,
):
    """Return overlap/rest conflicts with the preceding published block.

    Literal overlaps are always conflicts. Non-overlapping shifts are also
    conflicts when the physician's configured minimum rest would be violated,
    matching the authoritative swap validator.
    """
    starts = np.asarray(shift_starts, dtype=np.float64)
    ends = np.asarray(shift_ends, dtype=np.float64)
    rest = np.asarray(minimum_rest_seconds, dtype=np.float64)
    if rest.shape != (len(physician_ids),):
        raise ValueError(
            'Minimum-rest seconds must contain one value per physician.'
        )
    result = np.zeros((len(physician_ids), len(starts)), dtype=np.bool_)
    for physician_index, physician_id in enumerate(physician_ids):
        prior = tuple(boundary_context.get(int(physician_id), ()))
        if not prior:
            continue
        prior_starts = np.asarray(
            [row.start_datetime.timestamp() for row in prior],
            dtype=np.float64,
        )
        prior_ends = np.asarray(
            [row.end_datetime.timestamp() for row in prior],
            dtype=np.float64,
        )
        required_rest = max(float(rest[physician_index]), 0.0)
        result[physician_index] = np.any(
            (starts[:, None] < prior_ends[None, :] + required_rest)
            & (ends[:, None] > prior_starts[None, :] - required_rest),
            axis=1,
        )
    return result


@dataclass(frozen=True)
class V2CandidateSelection:
    candidate_index: int
    left_assignment_index: int
    right_assignment_index: int
    official_delta: float
    proportionality_delta: float


@dataclass(frozen=True)
class V2AssignmentRowCache:
    """Immutable bundle of assignment-indexed compiled scoring tables."""

    pairs: tuple
    tables: object

    @classmethod
    def create(cls, pairs, tables):
        normalized_pairs = tuple(tuple(map(int, pair)) for pair in pairs)
        normalized_tables = {}
        for name, rows in tables.items():
            values = np.asarray(rows)
            if values.shape[0] != len(normalized_pairs):
                raise ValueError(
                    f'Table {name} row count does not match assignment pairs.'
                )
            normalized_tables[str(name)] = values
        return cls(normalized_pairs, MappingProxyType(normalized_tables))

    def refreshed(self, new_pairs, refreshed_pairs, refreshed_tables):
        missing = set(self.tables) - set(refreshed_tables)
        extra = set(refreshed_tables) - set(self.tables)
        if missing or extra:
            raise ValueError(
                'Refreshed table names must exactly match cached table names.'
            )
        merged = {
            name: merge_assignment_rows(
                current_rows,
                self.pairs,
                new_pairs,
                refreshed_tables[name],
                refreshed_pairs,
            )
            for name, current_rows in self.tables.items()
        }
        return V2AssignmentRowCache.create(new_pairs, merged)


@dataclass(frozen=True)
class V2EngineContext:
    """Persistent immutable state for one in-process v2 search chain."""

    state: object
    assignment_rows: V2AssignmentRowCache
    seen_fingerprints: frozenset
    evaluated_schedules: int = 0
    accepted_transitions: int = 0

    @classmethod
    def create(cls, state, pairs, tables):
        copied_state = {
            int(instance_id): tuple(sorted(map(int, physician_ids)))
            for instance_id, physician_ids in state.items()
        }
        fingerprint = schedule_fingerprint(copied_state)
        return cls(
            state=MappingProxyType(copied_state),
            assignment_rows=V2AssignmentRowCache.create(pairs, tables),
            seen_fingerprints=frozenset((fingerprint,)),
        )

    @property
    def fingerprint(self):
        return schedule_fingerprint(self.state)

    def record_neighborhood(self, evaluated_schedules):
        evaluated = int(evaluated_schedules)
        if evaluated < 0:
            raise ValueError('Evaluated schedule count cannot be negative.')
        return V2EngineContext(
            state=self.state,
            assignment_rows=self.assignment_rows,
            seen_fingerprints=self.seen_fingerprints,
            evaluated_schedules=self.evaluated_schedules + evaluated,
            accepted_transitions=self.accepted_transitions,
        )

    def apply_selection(
        self,
        selection,
        new_pairs,
        refreshed_pairs,
        refreshed_tables,
    ):
        pairs = self.assignment_rows.pairs
        try:
            left_pair = pairs[int(selection.left_assignment_index)]
            right_pair = pairs[int(selection.right_assignment_index)]
        except (IndexError, TypeError) as exc:
            raise ValueError('Selected assignment index is outside the cache.') from exc
        new_state = swap_assignments(self.state, left_pair, right_pair)
        fingerprint = schedule_fingerprint(new_state)
        if fingerprint in self.seen_fingerprints:
            raise ValueError('The selected transition would revisit a prior state.')
        refreshed_cache = self.assignment_rows.refreshed(
            new_pairs,
            refreshed_pairs,
            refreshed_tables,
        )
        return V2EngineContext(
            state=MappingProxyType({
                instance_id: tuple(sorted(physician_ids))
                for instance_id, physician_ids in new_state.items()
            }),
            assignment_rows=refreshed_cache,
            seen_fingerprints=self.seen_fingerprints | frozenset((fingerprint,)),
            evaluated_schedules=self.evaluated_schedules,
            accepted_transitions=self.accepted_transitions + 1,
        )

    def apply_reassignment(
        self,
        instance_id,
        old_physician_id,
        new_physician_id,
        new_pairs,
        refreshed_pairs,
        refreshed_tables,
    ):
        """Apply one validated reassignment and refresh affected cache rows."""
        new_state = reassign_assignment(
            self.state,
            instance_id,
            old_physician_id,
            new_physician_id,
        )
        fingerprint = schedule_fingerprint(new_state)
        if fingerprint in self.seen_fingerprints:
            raise ValueError('The selected transition would revisit a prior state.')
        refreshed_cache = self.assignment_rows.refreshed(
            new_pairs,
            refreshed_pairs,
            refreshed_tables,
        )
        return V2EngineContext(
            state=MappingProxyType({
                current_instance_id: tuple(sorted(physician_ids))
                for current_instance_id, physician_ids in new_state.items()
            }),
            assignment_rows=refreshed_cache,
            seen_fingerprints=self.seen_fingerprints | frozenset((fingerprint,)),
            evaluated_schedules=self.evaluated_schedules,
            accepted_transitions=self.accepted_transitions + 1,
        )

    def apply_rotation(
        self,
        assignment_pairs,
        new_physician_ids,
        new_pairs,
        refreshed_pairs,
        refreshed_tables,
    ):
        """Apply one validated three-way rotation and refresh cache rows."""
        new_state = rotate_assignments(
            self.state, assignment_pairs, new_physician_ids,
        )
        fingerprint = schedule_fingerprint(new_state)
        if fingerprint in self.seen_fingerprints:
            raise ValueError('The selected transition would revisit a prior state.')
        refreshed_cache = self.assignment_rows.refreshed(
            new_pairs, refreshed_pairs, refreshed_tables,
        )
        return V2EngineContext(
            state=MappingProxyType({
                current_instance_id: tuple(sorted(physician_ids))
                for current_instance_id, physician_ids in new_state.items()
            }),
            assignment_rows=refreshed_cache,
            seen_fingerprints=self.seen_fingerprints | frozenset((fingerprint,)),
            evaluated_schedules=self.evaluated_schedules,
            accepted_transitions=self.accepted_transitions + 1,
        )

    def apply_reassignment_patch(
        self,
        reassignments,
        new_pairs,
        refreshed_pairs,
        refreshed_tables,
    ):
        """Apply one atomically validated multi-assignment reconstruction."""
        new_state = reassign_assignments(self.state, reassignments)
        fingerprint = schedule_fingerprint(new_state)
        if fingerprint in self.seen_fingerprints:
            raise ValueError('The selected transition would revisit a prior state.')
        refreshed_cache = self.assignment_rows.refreshed(
            new_pairs, refreshed_pairs, refreshed_tables,
        )
        return V2EngineContext(
            state=MappingProxyType({
                current_instance_id: tuple(sorted(physician_ids))
                for current_instance_id, physician_ids in new_state.items()
            }),
            assignment_rows=refreshed_cache,
            seen_fingerprints=self.seen_fingerprints | frozenset((fingerprint,)),
            evaluated_schedules=self.evaluated_schedules,
            accepted_transitions=self.accepted_transitions + 1,
        )


def schedule_fingerprint(state):
    """Return a deterministic compact identity for one complete assignment state."""
    digest = blake2b(digest_size=16)
    for instance_id in sorted(state):
        for physician_id in sorted(state.get(instance_id, ())):
            digest.update(int(instance_id).to_bytes(8, 'little', signed=False))
            digest.update(int(physician_id).to_bytes(8, 'little', signed=False))
    return digest.hexdigest()


def swap_assignments(state, left_pair, right_pair):
    """Return a copied state with a validated two-assignment swap applied."""
    left_instance_id, left_physician_id = map(int, left_pair)
    right_instance_id, right_physician_id = map(int, right_pair)
    if left_instance_id == right_instance_id:
        raise ValueError('A swap requires two different shift instances.')
    if left_physician_id == right_physician_id:
        raise ValueError('A swap requires two different physicians.')
    left_owners = list(state.get(left_instance_id, ()))
    right_owners = list(state.get(right_instance_id, ()))
    if left_physician_id not in left_owners:
        raise ValueError('The left assignment is not present in the current state.')
    if right_physician_id not in right_owners:
        raise ValueError('The right assignment is not present in the current state.')
    if right_physician_id in left_owners or left_physician_id in right_owners:
        raise ValueError('The swap would create a duplicate shift assignment.')
    copied = {
        int(instance_id): list(physician_ids)
        for instance_id, physician_ids in state.items()
    }
    copied[left_instance_id].remove(left_physician_id)
    copied[left_instance_id].append(right_physician_id)
    copied[right_instance_id].remove(right_physician_id)
    copied[right_instance_id].append(left_physician_id)
    return copied


def reassign_assignment(state, instance_id, old_physician_id, new_physician_id):
    """Return a copied state after one staffing-preserving reassignment.

    Unlike a pair swap, this operation may change each physician's assignment
    count and workload. It is the primitive required by V1's request,
    workload-transfer, and night-distribution tactics.
    """
    instance_id = int(instance_id)
    old_physician_id = int(old_physician_id)
    new_physician_id = int(new_physician_id)
    if old_physician_id == new_physician_id:
        raise ValueError('A reassignment requires two different physicians.')
    owners = list(state.get(instance_id, ()))
    if old_physician_id not in owners:
        raise ValueError('The outgoing assignment is not present in the state.')
    if new_physician_id in owners:
        raise ValueError('The reassignment would create a duplicate assignment.')
    copied = {
        int(current_instance_id): list(physician_ids)
        for current_instance_id, physician_ids in state.items()
    }
    copied[instance_id].remove(old_physician_id)
    copied[instance_id].append(new_physician_id)
    return copied


def reassign_assignments(state, reassignments):
    """Apply an ordered representation of one atomic reconstruction patch."""
    current = state
    for instance_id, old_physician_id, new_physician_id in reassignments:
        current = reassign_assignment(
            current, instance_id, old_physician_id, new_physician_id,
        )
    return current


def rotate_assignments(state, assignment_pairs, new_physician_ids):
    """Return a copied state after one simultaneous three-way rotation."""
    pairs = [tuple(map(int, pair)) for pair in assignment_pairs]
    replacements = [int(value) for value in new_physician_ids]
    if len(pairs) != 3 or len(replacements) != 3:
        raise ValueError('A rotation requires exactly three assignments.')
    instance_ids = [pair[0] for pair in pairs]
    old_physician_ids = [pair[1] for pair in pairs]
    if len(set(instance_ids)) != 3 or len(set(old_physician_ids)) != 3:
        raise ValueError('A rotation requires distinct shifts and physicians.')
    if set(replacements) != set(old_physician_ids):
        raise ValueError('A rotation must preserve the physician set.')
    if replacements == old_physician_ids:
        raise ValueError('A rotation cannot be a no-op.')
    copied = {
        int(current_instance_id): list(physician_ids)
        for current_instance_id, physician_ids in state.items()
    }
    for (instance_id, old_physician_id), new_physician_id in zip(
        pairs, replacements,
    ):
        owners = copied.get(instance_id, [])
        if old_physician_id not in owners:
            raise ValueError('A rotated assignment is absent from the state.')
        if new_physician_id != old_physician_id and new_physician_id in owners:
            raise ValueError('The rotation would create a duplicate assignment.')
    for instance_id, old_physician_id in pairs:
        copied[instance_id].remove(old_physician_id)
    for (instance_id, _old_physician_id), new_physician_id in zip(
        pairs, replacements,
    ):
        copied[instance_id].append(new_physician_id)
    return copied


def legal_reassignment_candidates(
    shift_for_assignment,
    physician_for_assignment,
    occupancy,
    eligible_facility,
    shift_facility,
    conflict_counts,
    boundary_conflict,
):
    """Return the hard-feasible staffing-preserving reassignments."""
    shifts = np.asarray(shift_for_assignment, dtype=np.int32)
    owners = np.asarray(physician_for_assignment, dtype=np.int32)
    occupancy = np.asarray(occupancy, dtype=np.bool_)
    eligible = np.asarray(eligible_facility, dtype=np.bool_)
    facilities = np.asarray(shift_facility, dtype=np.int32)
    conflicts = np.asarray(conflict_counts)
    boundary = np.asarray(boundary_conflict, dtype=np.bool_)
    physician_count, shift_count = occupancy.shape
    if shifts.shape != owners.shape:
        raise ValueError('Assignment shift and physician arrays must match.')
    if eligible.shape[0] != physician_count:
        raise ValueError('Eligibility must contain one row per physician.')
    if conflicts.shape != occupancy.shape or boundary.shape != occupancy.shape:
        raise ValueError('Conflict matrices must match occupancy.')
    if facilities.shape != (shift_count,):
        raise ValueError('Shift facilities must contain one value per shift.')

    assignment_batches = []
    replacement_batches = []
    all_physicians = np.arange(physician_count, dtype=np.int32)
    for assignment_index, (shift_index, owner_index) in enumerate(
        zip(shifts, owners)
    ):
        legal = (
            (all_physicians != owner_index)
            & ~occupancy[:, shift_index]
            & eligible[:, facilities[shift_index]]
            & (conflicts[:, shift_index] == 0)
            & ~boundary[:, shift_index]
        )
        replacements = np.flatnonzero(legal).astype(np.int32, copy=False)
        if not replacements.size:
            continue
        assignment_batches.append(np.full(
            replacements.size, assignment_index, dtype=np.int32,
        ))
        replacement_batches.append(replacements)
    if not assignment_batches:
        empty = np.asarray([], dtype=np.int32)
        return empty, empty
    return np.concatenate(assignment_batches), np.concatenate(replacement_batches)


def merge_assignment_rows(
    current_rows,
    current_pairs,
    new_pairs,
    refreshed_rows,
    refreshed_pairs,
):
    """Merge affected assignment rows into a newly ordered movable-pair table."""
    current = np.asarray(current_rows)
    refreshed = np.asarray(refreshed_rows)
    current_pairs = [tuple(map(int, pair)) for pair in current_pairs]
    new_pairs = [tuple(map(int, pair)) for pair in new_pairs]
    refreshed_pairs = [tuple(map(int, pair)) for pair in refreshed_pairs]
    if current.shape[0] != len(current_pairs):
        raise ValueError('Current row count does not match current assignment pairs.')
    if refreshed.shape[0] != len(refreshed_pairs):
        raise ValueError('Refreshed row count does not match refreshed assignment pairs.')
    if current.shape[1:] != refreshed.shape[1:]:
        raise ValueError('Current and refreshed assignment row shapes differ.')
    current_lookup = {pair: index for index, pair in enumerate(current_pairs)}
    refreshed_lookup = {pair: index for index, pair in enumerate(refreshed_pairs)}
    merged = np.empty((len(new_pairs), *current.shape[1:]), dtype=current.dtype)
    for new_index, pair in enumerate(new_pairs):
        refreshed_index = refreshed_lookup.get(pair)
        if refreshed_index is not None:
            merged[new_index] = refreshed[refreshed_index]
            continue
        current_index = current_lookup.get(pair)
        if current_index is None:
            raise ValueError(f'No current or refreshed row exists for assignment {pair}.')
        merged[new_index] = current[current_index]
    return merged


def merge_physician_rows(
    current_rows,
    physician_ids,
    refreshed_rows,
    refreshed_physician_ids,
):
    """Replace affected physician rows without changing global physician order."""
    current = np.asarray(current_rows).copy()
    refreshed = np.asarray(refreshed_rows)
    physician_ids = [int(physician_id) for physician_id in physician_ids]
    refreshed_physician_ids = [
        int(physician_id) for physician_id in refreshed_physician_ids
    ]
    if current.shape[0] != len(physician_ids):
        raise ValueError('Current row count does not match physician ids.')
    if refreshed.shape[0] != len(refreshed_physician_ids):
        raise ValueError('Refreshed row count does not match refreshed physician ids.')
    if current.shape[1:] != refreshed.shape[1:]:
        raise ValueError('Current and refreshed physician row shapes differ.')
    physician_lookup = {
        physician_id: index for index, physician_id in enumerate(physician_ids)
    }
    for refreshed_index, physician_id in enumerate(refreshed_physician_ids):
        current_index = physician_lookup.get(physician_id)
        if current_index is None:
            raise ValueError(f'Unknown refreshed physician {physician_id}.')
        current[current_index] = refreshed[refreshed_index]
    return current


def select_best_candidate(
    official_deltas,
    proportionality_deltas,
    left_assignment_indexes,
    right_assignment_indexes,
    *,
    tolerance=1e-9,
):
    """Select the best admissible v2 transition.

    A lower official penalty always wins. Proportionality may select a move only
    when the official penalty is unchanged within tolerance; it can never
    justify a worse official schedule.
    """
    official = np.asarray(official_deltas, dtype=np.float64)
    proportionality = np.asarray(proportionality_deltas, dtype=np.float64)
    left = np.asarray(left_assignment_indexes, dtype=np.int32)
    right = np.asarray(right_assignment_indexes, dtype=np.int32)
    if not (
        official.shape == proportionality.shape == left.shape == right.shape
    ):
        raise ValueError('Candidate arrays must have identical shapes.')
    acceptable = np.flatnonzero(
        (official < -tolerance)
        | (
            (np.abs(official) <= tolerance)
            & (proportionality < -tolerance)
        )
    )
    if not acceptable.size:
        return None
    ordering = np.lexsort((
        proportionality[acceptable],
        official[acceptable],
    ))
    candidate_index = int(acceptable[int(ordering[0])])
    return V2CandidateSelection(
        candidate_index=candidate_index,
        left_assignment_index=int(left[candidate_index]),
        right_assignment_index=int(right[candidate_index]),
        official_delta=float(official[candidate_index]),
        proportionality_delta=float(proportionality[candidate_index]),
    )


def select_diversification_candidate(
    official_deltas,
    proportionality_deltas,
    left_assignment_indexes,
    right_assignment_indexes,
    *,
    seed,
    minimum_penalty_increase=0.0,
    maximum_penalty_increase=10000.0,
    candidate_pool_size=256,
    tolerance=1e-9,
):
    """Select a bounded non-improving move for a protected-best restart.

    Diversification deliberately moves away from a pair-swap local optimum,
    but never by more than the configured penalty bound. The caller retains
    the global best state independently and validates hard legality exactly.
    """
    official = np.asarray(official_deltas, dtype=np.float64)
    proportionality = np.asarray(proportionality_deltas, dtype=np.float64)
    left = np.asarray(left_assignment_indexes, dtype=np.int32)
    right = np.asarray(right_assignment_indexes, dtype=np.int32)
    if not (
        official.shape == proportionality.shape == left.shape == right.shape
    ):
        raise ValueError('Candidate arrays must have identical shapes.')
    maximum = max(float(maximum_penalty_increase), 0.0)
    minimum = min(
        max(float(minimum_penalty_increase), 0.0),
        maximum,
    )
    admissible = np.flatnonzero(
        np.isfinite(official)
        & np.isfinite(proportionality)
        & (official >= minimum - tolerance)
        & (official <= maximum + tolerance)
        & (
            (official > tolerance)
            | (proportionality >= -tolerance)
        )
    )
    if not admissible.size:
        return None
    impactful = admissible[
        (official[admissible] > tolerance)
        | (np.abs(proportionality[admissible]) > tolerance)
    ]
    if impactful.size:
        admissible = impactful
    ordering = np.lexsort((
        proportionality[admissible],
        official[admissible],
    ))
    pool = admissible[ordering[:max(int(candidate_pool_size), 1)]]
    rng = np.random.default_rng(int(seed))
    candidate_index = int(pool[int(rng.integers(0, len(pool)))])
    return V2CandidateSelection(
        candidate_index=candidate_index,
        left_assignment_index=int(left[candidate_index]),
        right_assignment_index=int(right[candidate_index]),
        official_delta=float(official[candidate_index]),
        proportionality_delta=float(proportionality[candidate_index]),
    )
