from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
from django.test import SimpleTestCase

from apps.scheduling.management.commands.benchmark_v2_swap_kernel import (
    Command as SwapKernelCommand,
    _request_reassignment_deltas,
)
from apps.scheduling.optimizer_v2 import (
    V2AssignmentRowCache,
    V2CandidateSelection,
    V2EngineContext,
    legal_reassignment_candidates,
    merge_assignment_rows,
    merge_physician_rows,
    published_boundary_conflict_matrix,
    published_boundary_overlap_matrix,
    schedule_fingerprint,
    reassign_assignment,
    rotate_assignments,
    select_best_candidate,
    select_diversification_candidate,
    swap_assignments,
)
from apps.scheduling.optimizer_v2_runner import (
    _apply_search_operations,
    _authoritative_search_initial_score,
    _require_hard_valid_final_report,
    _require_matching_final_score,
)


class V2SwapKernelCommandTests(SimpleTestCase):
    def test_request_reassignment_delta_removes_and_adds_request_cost(self):
        kernel = {
            'off_cost': np.asarray([[100.0], [0.0]]),
            'on_match': np.zeros((2, 0, 1), dtype=np.int8),
            'on_counts': np.zeros((2, 0), dtype=np.int16),
            'on_weights': np.zeros((2, 0), dtype=np.float64),
        }

        deltas = _request_reassignment_deltas(
            kernel,
            outgoing_physicians=np.asarray([0]),
            incoming_physicians=np.asarray([1]),
            shifts=np.asarray([0]),
        )

        np.testing.assert_array_equal(deltas, np.asarray([-100.0]))

    def test_accumulated_refresh_audit_is_opt_in(self):
        parser = SwapKernelCommand().create_parser(
            'manage.py', 'benchmark_v2_swap_kernel',
        )

        default_options = parser.parse_args(['--run-number', '95'])
        audit_options = parser.parse_args([
            '--run-number', '95', '--validate-refresh',
        ])

        self.assertFalse(default_options.validate_refresh)
        self.assertTrue(audit_options.validate_refresh)


class V2FinalScoreGuardTests(SimpleTestCase):
    def test_opening_checkpoint_replaces_stale_bootstrap_score(self):
        self.assertEqual(
            _authoritative_search_initial_score({
                'score_checkpoints': [{
                    'accepted_transitions': 0,
                    'score': 2656000,
                }],
            }, Decimal('2500000')),
            Decimal('2656000'),
        )

    def test_operation_replay_preserves_order_across_move_types(self):
        state = _apply_search_operations(
            {10: [1], 20: [2], 30: [3]},
            [
                '10:1:20:2',
                'R:30:3:4',
                'C:10:2:1:20:1:4:30:4:2',
            ],
        )

        self.assertEqual(state, {10: [1], 20: [4], 30: [2]})

    def test_matching_authoritative_final_score_is_accepted(self):
        _require_matching_final_score(
            Decimal('791000'), {'predicted_final_score': 791000.0},
        )

    def test_missing_or_divergent_final_score_is_rejected(self):
        for search_result in (
            {},
            {'predicted_final_score': 790999.0},
        ):
            with self.subTest(search_result=search_result):
                with self.assertRaisesMessage(
                    ValueError, 'diverged from the final authoritative score',
                ):
                    _require_matching_final_score(
                        Decimal('791000'), search_result,
                    )

    def test_hard_invalid_final_reports_are_rejected(self):
        cases = (
            ('overlap_score', 'time-overlap conflict'),
            ('invalid_assignment_score', 'optimizer-ineligible assignment'),
            ('coverage_score', 'incomplete schedule'),
        )
        for score_name, message in cases:
            with self.subTest(score_name=score_name):
                with self.assertRaisesMessage(ValueError, message):
                    _require_hard_valid_final_report({
                        'score_breakdown': {score_name: 1},
                    })

    def test_zero_hard_scores_are_accepted(self):
        _require_hard_valid_final_report({
            'score_breakdown': {
                'overlap_score': 0,
                'invalid_assignment_score': 0,
                'coverage_score': 0,
            },
        })


class PublishedBoundaryOverlapMatrixTests(SimpleTestCase):
    @staticmethod
    def _timestamp(hour, minute=0):
        return datetime(
            2027, 2, 1, hour, minute, tzinfo=timezone.utc,
        ).timestamp()

    def test_positive_time_intersection_is_a_hard_conflict(self):
        boundary_context = {
            101: (
                SimpleNamespace(
                    start_datetime=datetime(
                        2027, 1, 31, 22, 0, tzinfo=timezone.utc,
                    ),
                    end_datetime=datetime(
                        2027, 2, 1, 6, 0, tzinfo=timezone.utc,
                    ),
                ),
            ),
        }

        matrix = published_boundary_overlap_matrix(
            physician_ids=[101, 202],
            shift_starts=np.asarray([
                self._timestamp(5, 30),
                self._timestamp(6),
                self._timestamp(14),
            ]),
            shift_ends=np.asarray([
                self._timestamp(14, 30),
                self._timestamp(14),
                self._timestamp(22),
            ]),
            boundary_context=boundary_context,
        )

        np.testing.assert_array_equal(
            matrix,
            np.asarray([
                [True, False, False],
                [False, False, False],
            ]),
        )

    def test_touching_endpoints_are_legal(self):
        boundary_context = {
            101: (
                SimpleNamespace(
                    start_datetime=datetime(
                        2027, 2, 1, 22, 0, tzinfo=timezone.utc,
                    ),
                    end_datetime=datetime(
                        2027, 2, 2, 6, 0, tzinfo=timezone.utc,
                    ),
                ),
            ),
        }

        matrix = published_boundary_overlap_matrix(
            physician_ids=[101],
            shift_starts=np.asarray([
                self._timestamp(14),
                datetime(
                    2027, 2, 2, 6, 0, tzinfo=timezone.utc,
                ).timestamp(),
            ]),
            shift_ends=np.asarray([
                self._timestamp(22),
                datetime(
                    2027, 2, 2, 14, 0, tzinfo=timezone.utc,
                ).timestamp(),
            ]),
            boundary_context=boundary_context,
        )

        np.testing.assert_array_equal(matrix, np.asarray([[False, False]]))

    def test_configured_rest_expands_the_published_boundary_conflict(self):
        boundary_context = {
            101: (
                SimpleNamespace(
                    start_datetime=datetime(
                        2027, 1, 31, 22, 0, tzinfo=timezone.utc,
                    ),
                    end_datetime=datetime(
                        2027, 2, 1, 6, 0, tzinfo=timezone.utc,
                    ),
                ),
            ),
        }

        matrix = published_boundary_conflict_matrix(
            physician_ids=[101, 202],
            shift_starts=np.asarray([
                self._timestamp(6),
                self._timestamp(16),
            ]),
            shift_ends=np.asarray([
                self._timestamp(14),
                datetime(
                    2027, 2, 2, 0, 0, tzinfo=timezone.utc,
                ).timestamp(),
            ]),
            boundary_context=boundary_context,
            minimum_rest_seconds=np.asarray([10 * 3600, 10 * 3600]),
        )

        np.testing.assert_array_equal(
            matrix,
            np.asarray([
                [True, False],
                [False, False],
            ]),
        )

    def test_boundary_conflict_requires_one_rest_value_per_physician(self):
        with self.assertRaisesMessage(
            ValueError, 'one value per physician',
        ):
            published_boundary_conflict_matrix(
                physician_ids=[101],
                shift_starts=np.asarray([self._timestamp(6)]),
                shift_ends=np.asarray([self._timestamp(14)]),
                boundary_context={},
                minimum_rest_seconds=np.asarray([]),
            )


class V2CandidateSelectionTests(SimpleTestCase):
    def test_diversification_is_bounded_and_skips_improving_moves(self):
        selected = select_diversification_candidate(
            official_deltas=np.asarray([-10.0, 0.0, 5000.0, 15000.0]),
            proportionality_deltas=np.asarray([1.0, -1.0, 2.0, 3.0]),
            left_assignment_indexes=np.asarray([1, 2, 3, 4]),
            right_assignment_indexes=np.asarray([11, 12, 13, 14]),
            seed=123,
            maximum_penalty_increase=10000,
        )

        self.assertEqual(selected.candidate_index, 2)
        self.assertEqual(selected.official_delta, 5000.0)

    def test_diversification_can_use_score_neutral_nonimproving_move(self):
        selected = select_diversification_candidate(
            official_deltas=np.asarray([0.0]),
            proportionality_deltas=np.asarray([0.5]),
            left_assignment_indexes=np.asarray([3]),
            right_assignment_indexes=np.asarray([13]),
            seed=123,
            maximum_penalty_increase=0,
        )

        self.assertEqual(selected.candidate_index, 0)

    def test_diversification_can_require_crossing_a_penalty_barrier(self):
        selected = select_diversification_candidate(
            official_deltas=np.asarray([0.0, 1000.0, 5000.0]),
            proportionality_deltas=np.asarray([5.0, 2.0, 1.0]),
            left_assignment_indexes=np.asarray([1, 2, 3]),
            right_assignment_indexes=np.asarray([11, 12, 13]),
            seed=1,
            minimum_penalty_increase=1.0,
            maximum_penalty_increase=5000.0,
        )

        self.assertIsNotNone(selected)
        self.assertGreater(selected.official_delta, 0.0)

    def test_diversification_skips_structurally_inert_move_when_possible(self):
        selected = select_diversification_candidate(
            official_deltas=np.asarray([0.0, 0.0]),
            proportionality_deltas=np.asarray([0.0, 0.5]),
            left_assignment_indexes=np.asarray([3, 4]),
            right_assignment_indexes=np.asarray([13, 14]),
            seed=123,
            maximum_penalty_increase=0,
        )

        self.assertEqual(selected.candidate_index, 1)

    def test_lower_official_penalty_dominates_proportionality(self):
        selected = select_best_candidate(
            official_deltas=np.asarray([-5.0, -10.0, 0.0]),
            proportionality_deltas=np.asarray([-100.0, 50.0, -200.0]),
            left_assignment_indexes=np.asarray([1, 2, 3]),
            right_assignment_indexes=np.asarray([11, 12, 13]),
        )

        self.assertEqual(selected.candidate_index, 1)
        self.assertEqual(selected.left_assignment_index, 2)
        self.assertEqual(selected.right_assignment_index, 12)
        self.assertEqual(selected.official_delta, -10.0)

    def test_proportionality_breaks_official_score_tie(self):
        selected = select_best_candidate(
            official_deltas=np.asarray([0.0, 0.0, 5.0]),
            proportionality_deltas=np.asarray([-1.0, -3.0, -100.0]),
            left_assignment_indexes=np.asarray([1, 2, 3]),
            right_assignment_indexes=np.asarray([11, 12, 13]),
        )

        self.assertEqual(selected.candidate_index, 1)
        self.assertEqual(selected.proportionality_delta, -3.0)

    def test_worse_official_score_is_never_accepted(self):
        selected = select_best_candidate(
            official_deltas=np.asarray([1.0, 5.0]),
            proportionality_deltas=np.asarray([-100.0, -1000.0]),
            left_assignment_indexes=np.asarray([1, 2]),
            right_assignment_indexes=np.asarray([11, 12]),
        )

        self.assertIsNone(selected)

    def test_neutral_nonimproving_candidates_are_not_accepted(self):
        selected = select_best_candidate(
            official_deltas=np.asarray([0.0, 0.0]),
            proportionality_deltas=np.asarray([0.0, 1.0]),
            left_assignment_indexes=np.asarray([1, 2]),
            right_assignment_indexes=np.asarray([11, 12]),
        )

        self.assertIsNone(selected)

    def test_mismatched_candidate_shapes_are_rejected(self):
        with self.assertRaises(ValueError):
            select_best_candidate(
                official_deltas=np.asarray([0.0]),
                proportionality_deltas=np.asarray([0.0, 1.0]),
                left_assignment_indexes=np.asarray([1]),
                right_assignment_indexes=np.asarray([11]),
            )


class V2StateTransitionTests(SimpleTestCase):
    def setUp(self):
        self.state = {10: [1, 3], 20: [2], 30: [4]}

    def test_swap_returns_independent_state_and_preserves_staffing(self):
        swapped = swap_assignments(self.state, (10, 1), (20, 2))

        self.assertEqual(self.state, {10: [1, 3], 20: [2], 30: [4]})
        self.assertCountEqual(swapped[10], [2, 3])
        self.assertEqual(swapped[20], [1])
        self.assertEqual(len(swapped[10]), len(self.state[10]))
        self.assertEqual(len(swapped[20]), len(self.state[20]))

    def test_swap_rejects_missing_or_duplicate_assignments(self):
        with self.assertRaises(ValueError):
            swap_assignments(self.state, (10, 99), (20, 2))
        with self.assertRaises(ValueError):
            swap_assignments(self.state, (10, 1), (10, 3))
        with self.assertRaises(ValueError):
            swap_assignments({10: [1, 2], 20: [2]}, (10, 1), (20, 2))

    def test_reassignment_changes_one_owner_and_preserves_staffing(self):
        reassigned = reassign_assignment(self.state, 10, 1, 2)

        self.assertEqual(self.state, {10: [1, 3], 20: [2], 30: [4]})
        self.assertCountEqual(reassigned[10], [2, 3])
        self.assertEqual(len(reassigned[10]), len(self.state[10]))

    def test_reassignment_rejects_missing_noop_and_duplicate_owner(self):
        with self.assertRaises(ValueError):
            reassign_assignment(self.state, 10, 99, 2)
        with self.assertRaises(ValueError):
            reassign_assignment(self.state, 10, 1, 1)
        with self.assertRaises(ValueError):
            reassign_assignment(self.state, 10, 1, 3)

    def test_reassignment_candidates_enforce_hard_constraints(self):
        assignment_indexes, replacements = legal_reassignment_candidates(
            shift_for_assignment=np.asarray([0, 1]),
            physician_for_assignment=np.asarray([0, 1]),
            occupancy=np.asarray([
                [True, False],
                [False, True],
                [False, False],
                [False, False],
            ]),
            eligible_facility=np.asarray([
                [True, True],
                [True, True],
                [False, False],
                [True, True],
            ]),
            shift_facility=np.asarray([0, 1]),
            conflict_counts=np.asarray([
                [1, 0],
                [1, 1],
                [0, 0],
                [1, 0],
            ]),
            boundary_conflict=np.asarray([
                [False, False],
                [False, False],
                [False, False],
                [False, True],
            ]),
        )

        self.assertEqual(
            list(zip(assignment_indexes.tolist(), replacements.tolist())),
            [(1, 0)],
        )

    def test_rotation_is_atomic_and_preserves_staffing(self):
        rotated = rotate_assignments(
            self.state,
            [(10, 1), (20, 2), (30, 4)],
            [2, 4, 1],
        )

        self.assertEqual(self.state, {10: [1, 3], 20: [2], 30: [4]})
        self.assertCountEqual(rotated[10], [2, 3])
        self.assertEqual(rotated[20], [4])
        self.assertEqual(rotated[30], [1])

    def test_rotation_rejects_invalid_membership_and_duplicates(self):
        with self.assertRaises(ValueError):
            rotate_assignments(
                self.state,
                [(10, 99), (20, 2), (30, 4)],
                [2, 4, 99],
            )
        with self.assertRaises(ValueError):
            rotate_assignments(
                self.state,
                [(10, 1), (20, 2), (30, 4)],
                [1, 2, 4],
            )

    def test_schedule_fingerprint_is_order_independent_and_state_sensitive(self):
        reordered = {30: [4], 20: [2], 10: [3, 1]}
        swapped = swap_assignments(self.state, (10, 1), (20, 2))

        self.assertEqual(
            schedule_fingerprint(self.state), schedule_fingerprint(reordered),
        )
        self.assertNotEqual(
            schedule_fingerprint(self.state), schedule_fingerprint(swapped),
        )


class V2IncrementalMergeTests(SimpleTestCase):
    def test_assignment_rows_follow_new_ownership_and_order(self):
        current_pairs = [(10, 1), (20, 2), (30, 3)]
        current_rows = np.asarray([[10, 1], [20, 2], [30, 3]])
        new_pairs = [(10, 2), (20, 1), (30, 3)]
        refreshed_pairs = [(10, 2), (20, 1)]
        refreshed_rows = np.asarray([[100, 2], [200, 1]])

        merged = merge_assignment_rows(
            current_rows,
            current_pairs,
            new_pairs,
            refreshed_rows,
            refreshed_pairs,
        )

        np.testing.assert_array_equal(
            merged,
            np.asarray([[100, 2], [200, 1], [30, 3]]),
        )

    def test_assignment_rows_reject_unknown_or_incompatible_rows(self):
        with self.assertRaises(ValueError):
            merge_assignment_rows(
                np.zeros((1, 2)), [(10, 1)], [(20, 2)],
                np.zeros((0, 2)), [],
            )
        with self.assertRaises(ValueError):
            merge_assignment_rows(
                np.zeros((1, 2)), [(10, 1)], [(10, 1)],
                np.zeros((1, 3)), [(10, 1)],
            )

    def test_physician_rows_replace_only_affected_physicians(self):
        merged = merge_physician_rows(
            np.asarray([[1, 10], [2, 20], [3, 30]]),
            [100, 200, 300],
            np.asarray([[30, 300], [10, 100]]),
            [300, 100],
        )

        np.testing.assert_array_equal(
            merged,
            np.asarray([[10, 100], [2, 20], [30, 300]]),
        )

    def test_physician_rows_reject_unknown_physician(self):
        with self.assertRaises(ValueError):
            merge_physician_rows(
                np.zeros((1, 2)), [100], np.ones((1, 2)), [999],
            )

    def test_assignment_cache_refreshes_all_tables_atomically(self):
        cache = V2AssignmentRowCache.create(
            [(10, 1), (20, 2), (30, 3)],
            {
                'workload': np.asarray([[1], [2], [3]]),
                'weekend': np.asarray([[10], [20], [30]]),
            },
        )

        refreshed = cache.refreshed(
            [(10, 2), (20, 1), (30, 3)],
            [(10, 2), (20, 1)],
            {
                'workload': np.asarray([[100], [200]]),
                'weekend': np.asarray([[1000], [2000]]),
            },
        )

        self.assertEqual(refreshed.pairs, ((10, 2), (20, 1), (30, 3)))
        np.testing.assert_array_equal(
            refreshed.tables['workload'], np.asarray([[100], [200], [3]]),
        )
        np.testing.assert_array_equal(
            refreshed.tables['weekend'], np.asarray([[1000], [2000], [30]]),
        )

    def test_assignment_cache_rejects_partial_refresh(self):
        cache = V2AssignmentRowCache.create(
            [(10, 1)],
            {'workload': np.asarray([[1]]), 'weekend': np.asarray([[10]])},
        )

        with self.assertRaises(ValueError):
            cache.refreshed(
                [(10, 2)], [(10, 2)], {'workload': np.asarray([[2]])},
            )


class V2EngineContextTests(SimpleTestCase):
    def setUp(self):
        self.context = V2EngineContext.create(
            {10: [1], 20: [2], 30: [3]},
            [(10, 1), (20, 2), (30, 3)],
            {
                'workload': np.asarray([[1], [2], [3]]),
                'weekend': np.asarray([[10], [20], [30]]),
            },
        )
        self.selection = V2CandidateSelection(
            candidate_index=0,
            left_assignment_index=0,
            right_assignment_index=1,
            official_delta=-10.0,
            proportionality_delta=0.0,
        )

    def test_context_applies_selection_and_refreshes_cache(self):
        recorded = self.context.record_neighborhood(1_832_317)
        updated = recorded.apply_selection(
            self.selection,
            [(10, 2), (20, 1), (30, 3)],
            [(10, 2), (20, 1)],
            {
                'workload': np.asarray([[100], [200]]),
                'weekend': np.asarray([[1000], [2000]]),
            },
        )

        self.assertEqual(updated.evaluated_schedules, 1_832_317)
        self.assertEqual(updated.accepted_transitions, 1)
        self.assertEqual(len(updated.seen_fingerprints), 2)
        self.assertEqual(updated.state[10], (2,))
        self.assertEqual(updated.state[20], (1,))
        np.testing.assert_array_equal(
            updated.assignment_rows.tables['workload'],
            np.asarray([[100], [200], [3]]),
        )

    def test_context_applies_reassignment_and_refreshes_cache(self):
        recorded = self.context.record_neighborhood(27)
        updated = recorded.apply_reassignment(
            10,
            1,
            2,
            [(10, 2), (20, 2), (30, 3)],
            [(10, 2), (20, 2)],
            {
                'workload': np.asarray([[100], [200]]),
                'weekend': np.asarray([[1000], [2000]]),
            },
        )

        self.assertEqual(updated.evaluated_schedules, 27)
        self.assertEqual(updated.accepted_transitions, 1)
        self.assertEqual(updated.state[10], (2,))
        self.assertEqual(updated.state[20], (2,))
        np.testing.assert_array_equal(
            updated.assignment_rows.tables['workload'],
            np.asarray([[100], [200], [3]]),
        )

    def test_context_applies_three_way_rotation(self):
        updated = self.context.apply_rotation(
            [(10, 1), (20, 2), (30, 3)],
            [2, 3, 1],
            [(10, 2), (20, 3), (30, 1)],
            [(10, 2), (20, 3), (30, 1)],
            {
                'workload': np.asarray([[100], [200], [300]]),
                'weekend': np.asarray([[1000], [2000], [3000]]),
            },
        )

        self.assertEqual(updated.state[10], (2,))
        self.assertEqual(updated.state[20], (3,))
        self.assertEqual(updated.state[30], (1,))
        self.assertEqual(updated.accepted_transitions, 1)

    def test_context_applies_atomic_reconstruction_patch(self):
        updated = self.context.apply_reassignment_patch(
            [(10, 1, 2), (20, 2, 3), (30, 3, 1)],
            [(10, 2), (20, 3), (30, 1)],
            [(10, 2), (20, 3), (30, 1)],
            {
                'workload': np.asarray([[100], [200], [300]]),
                'weekend': np.asarray([[1000], [2000], [3000]]),
            },
        )

        self.assertEqual(updated.state[10], (2,))
        self.assertEqual(updated.state[20], (3,))
        self.assertEqual(updated.state[30], (1,))
        self.assertEqual(updated.accepted_transitions, 1)

    def test_context_rejects_cycle_and_invalid_evaluation_count(self):
        with self.assertRaises(ValueError):
            self.context.record_neighborhood(-1)
        cycled = V2EngineContext(
            state=self.context.state,
            assignment_rows=self.context.assignment_rows,
            seen_fingerprints=self.context.seen_fingerprints | frozenset((
                schedule_fingerprint({10: [2], 20: [1], 30: [3]}),
            )),
        )
        with self.assertRaises(ValueError):
            cycled.apply_selection(
                self.selection,
                [(10, 2), (20, 1), (30, 3)],
                [(10, 2), (20, 1)],
                {
                    'workload': np.asarray([[100], [200]]),
                    'weekend': np.asarray([[1000], [2000]]),
                },
            )
