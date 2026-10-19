from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
from django.test import SimpleTestCase

from apps.scheduling.management.commands.benchmark_v2_swap_kernel import (
    Command as SwapKernelCommand,
    _pair_validation_penalty_deltas,
    _prepare_circadian_kernel,
    _prepare_shift_rule_kernel,
    _request_reassignment_deltas,
    _requests_active_under_block_plans,
    _shift_eligibility_matrix,
    _same_shift_score,
    _state_with_all_instances,
    _streak_penalty_from_days,
    _structural_focus_family,
)
from apps.scheduling.optimizer_v2 import (
    V2AssignmentRowCache,
    V2CandidateSelection,
    V2EngineContext,
    legal_reassignment_candidates,
    internal_conflict_count_matrices,
    merge_assignment_rows,
    merge_physician_rows,
    published_boundary_conflict_count_matrices,
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
    _compact_search_summary,
    _require_hard_valid_final_report,
    _require_matching_final_score,
)
from apps.scheduling.optimizer_telemetry import ProductionOptimizerTelemetry
from apps.scheduling.optimizer_revisions import (
    AVAILABILITY_V2_REVISION,
    CIRCADIAN_V2_REVISION,
    STABLE_V2_REVISION,
    TEMPORAL_V2_REVISION,
    configuration_digest,
    require_supported_v2_revision,
)


class OptimizerRevisionTests(SimpleTestCase):
    def test_configuration_digest_is_canonical(self):
        left = configuration_digest({
            'contracts': [{'id': 2, 'settings': {'b': 2, 'a': 1}}],
            'seed': 9,
            'captured_at': '2026-10-17T10:00:00Z',
            'source_assignments': [{
                'id': 100, 'shift_instance_id': 7, 'physician_id': 8,
            }],
        })
        right = configuration_digest({
            'seed': 9,
            'contracts': [{'settings': {'a': 1, 'b': 2}, 'id': 2}],
            'captured_at': '2026-10-17T11:00:00Z',
            'source_assignments': [{
                'id': 200, 'physician_id': 8, 'shift_instance_id': 7,
            }],
        })

        self.assertEqual(left, right)
        self.assertEqual(len(left), 64)

    def test_unknown_revision_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'not supported'):
            require_supported_v2_revision('v2.unapproved')

    def test_stable_revision_is_supported(self):
        self.assertEqual(
            require_supported_v2_revision(STABLE_V2_REVISION),
            STABLE_V2_REVISION,
        )

    def test_circadian_candidate_revision_is_supported(self):
        self.assertEqual(
            require_supported_v2_revision(CIRCADIAN_V2_REVISION),
            CIRCADIAN_V2_REVISION,
        )

    def test_availability_candidate_revision_is_supported(self):
        self.assertEqual(
            require_supported_v2_revision(AVAILABILITY_V2_REVISION),
            AVAILABILITY_V2_REVISION,
        )

    def test_temporal_candidate_revision_is_supported(self):
        self.assertEqual(
            require_supported_v2_revision(TEMPORAL_V2_REVISION),
            TEMPORAL_V2_REVISION,
        )

    def test_circadian_violation_uses_temporal_structural_family(self):
        self.assertEqual(
            _structural_focus_family('CIRCADIAN_BACKWARD_ROTATION'),
            'circadian',
        )


class V2SwapKernelCommandTests(SimpleTestCase):
    @staticmethod
    def _circadian_instance(instance_id, day, hour):
        start = datetime(
            day.year, day.month, day.day, hour, tzinfo=timezone.utc,
        )
        return SimpleNamespace(
            id=instance_id,
            date=day,
            start_datetime=start,
            end_datetime=start + timedelta(hours=8),
        )

    def test_partial_block_unavailable_dates_remove_requests_from_scoring(self):
        active_request = SimpleNamespace(
            physician_id=1, date=date(2027, 2, 2),
        )
        unavailable_request = SimpleNamespace(
            physician_id=1, date=date(2027, 2, 1),
        )
        unaffected_request = SimpleNamespace(
            physician_id=2, date=date(2027, 2, 1),
        )
        contracts = {
            1: SimpleNamespace(block_unavailable_dates={'2027-02-01'}),
            2: SimpleNamespace(block_unavailable_dates=set()),
        }

        result = _requests_active_under_block_plans(
            [unavailable_request, active_request, unaffected_request],
            contracts,
        )

        self.assertEqual(result, [active_request, unaffected_request])

    def test_shift_eligibility_combines_facilities_and_unavailable_dates(self):
        instances = [
            SimpleNamespace(date=date(2027, 2, 1)),
            SimpleNamespace(date=date(2027, 2, 2)),
            SimpleNamespace(date=date(2027, 2, 1)),
        ]
        eligible = _shift_eligibility_matrix(
            instances=instances,
            physician_ids=[10, 20],
            contracts={
                10: SimpleNamespace(block_unavailable_dates={'2027-02-01'}),
                20: SimpleNamespace(block_unavailable_dates=set()),
            },
            eligible_facility=np.asarray([
                [True, True],
                [True, False],
            ]),
            shift_facility=np.asarray([0, 0, 1]),
        )

        np.testing.assert_array_equal(eligible, np.asarray([
            [False, True, False],
            [True, True, False],
        ]))

    def test_circadian_kernel_scores_backward_replacement(self):
        instances = [
            self._circadian_instance(10, date(2026, 1, 1), 10),
            self._circadian_instance(20, date(2026, 1, 2), 9),
            self._circadian_instance(30, date(2026, 1, 2), 8),
        ]
        occupancy = np.asarray([[True, True, False]])
        contract = SimpleNamespace(workload_settings={
            'circadian_enabled': True,
            'circadian_penalty_weight': 100,
        })
        kernel = _prepare_circadian_kernel(
            instances,
            occupancy,
            [1],
            {1: contract},
            [(20, 1)],
            {instance.id: index for index, instance in enumerate(instances)},
        )

        self.assertEqual(kernel['replacement_by_assignment'][0, 1], 0.0)
        self.assertEqual(kernel['replacement_by_assignment'][0, 2], 100.0)

    def test_circadian_kernel_removes_published_boundary_violation(self):
        instances = [
            self._circadian_instance(20, date(2026, 1, 2), 8),
            self._circadian_instance(30, date(2026, 1, 2), 10),
        ]
        prior = self._circadian_instance(10, date(2026, 1, 1), 10)
        instances[0]._published_boundary_context = {1: (prior,)}
        occupancy = np.asarray([[True, False]])
        contract = SimpleNamespace(workload_settings={
            'circadian_enabled': True,
            'circadian_penalty_weight': 100,
        })
        kernel = _prepare_circadian_kernel(
            instances,
            occupancy,
            [1],
            {1: contract},
            [(20, 1)],
            {instance.id: index for index, instance in enumerate(instances)},
        )
        self.assertEqual(kernel['replacement_by_assignment'][0, 1], -100.0)

    def test_shift_rule_kernel_includes_group_same_shift_streak(self):
        instances = [
            self._circadian_instance(10, date(2026, 1, 1), 8),
            self._circadian_instance(20, date(2026, 1, 2), 8),
            self._circadian_instance(30, date(2026, 1, 3), 8),
            self._circadian_instance(40, date(2026, 1, 3), 16),
        ]
        for index, instance in enumerate(instances):
            instance.shift_template_id = 1 if index < 3 else 2
            instance.facility_id = 1
        occupancy = np.asarray([[True, True, True, False]])
        contract = SimpleNamespace(
            shift_settings={'rules': [{
                'shift_template_ids': [1],
                'max_same_shifts_in_row': 2,
                'max_same_shifts_in_row_penalty_weight': 100,
            }]},
            facility_settings={},
        )
        kernel = _prepare_shift_rule_kernel(
            instances,
            occupancy,
            [1],
            {1: contract},
            np.asarray([0, 1, 2], dtype=np.int32),
            np.asarray([0, 0, 0], dtype=np.int16),
            include_group_streaks=True,
        )
        stable_kernel = _prepare_shift_rule_kernel(
            instances,
            occupancy,
            [1],
            {1: contract},
            np.asarray([0, 1, 2], dtype=np.int32),
            np.asarray([0, 0, 0], dtype=np.int16),
        )

        self.assertEqual(kernel[2, 2], 0.0)
        self.assertEqual(kernel[2, 3], -100.0)
        self.assertEqual(stable_kernel[2, 3], 0.0)

    def test_compiled_adjacent_streak_penalties_are_flat_per_streak(self):
        self.assertEqual(
            _streak_penalty_from_days(
                {1, 2, 3, 4}, None, 2, 0.0, 100.0,
            ),
            100.0,
        )
        self.assertEqual(
            _streak_penalty_from_days(
                {1}, 2, None, 50.0, 0.0,
            ),
            50.0,
        )
        self.assertEqual(_same_shift_score([0, 1, 2, 3], 2, 100.0), 100.0)

    def test_newly_open_shift_survives_plain_dict_transition(self):
        state = _state_with_all_instances(
            {10: [1], 20: [2]},
            [
                SimpleNamespace(id=10),
                SimpleNamespace(id=20),
                SimpleNamespace(id=30),
            ],
        )

        transitioned = swap_assignments(state, (10, 1), (20, 2))

        self.assertEqual(transitioned[30], [])

    def test_pair_delta_credits_removed_rest_conflict_without_rescoring(self):
        occupancy = np.asarray([
            [True, True, False],
            [False, False, True],
        ])
        starts = np.asarray([0, 12, 30], dtype=np.float64) * 3600
        ends = np.asarray([8, 20, 38], dtype=np.float64) * 3600
        overlap_counts, rest_counts = internal_conflict_count_matrices(
            occupancy,
            starts,
            ends,
            np.asarray([10, 10], dtype=np.float64) * 3600,
        )
        empty_boundary_counts = np.zeros_like(overlap_counts)

        overlap_deltas, rest_deltas = _pair_validation_penalty_deltas(
            overlap_counts,
            rest_counts,
            empty_boundary_counts,
            empty_boundary_counts,
            shift_for_assignment=np.asarray([0, 1, 2]),
            physician_for_assignment=np.asarray([0, 0, 1]),
            left_assignment_indexes=np.asarray([0]),
            right_assignment_indexes=np.asarray([2]),
        )

        np.testing.assert_array_equal(overlap_deltas, np.asarray([0.0]))
        np.testing.assert_array_equal(rest_deltas, np.asarray([-10000.0]))

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
    def test_completed_summary_retains_bounded_score_audit(self):
        checkpoints = [
            {'accepted_transitions': index, 'score': 1000 - index}
            for index in range(300)
        ]
        compact = _compact_search_summary({
            'predicted_final_score': 701,
            'score_checkpoint_interval': 20,
            'score_checkpoints': checkpoints,
            'best_authoritative_checkpoint': {
                'accepted_transitions': 299,
                'score': 701,
                'schedule_fingerprint': 'verified-best',
                'operation_count': 299,
                'operation_chain': ['large replay evidence is omitted'],
            },
            'neighborhoods': [{'large': 'diagnostic payload'}],
        })

        self.assertEqual(len(compact['score_checkpoints']), 256)
        self.assertEqual(compact['score_checkpoints'][0], checkpoints[0])
        self.assertEqual(compact['score_checkpoints'][-1], checkpoints[-1])
        self.assertEqual(compact['score_checkpoints_omitted'], 44)
        self.assertEqual(compact['best_authoritative_checkpoint'], {
            'accepted_transitions': 299,
            'score': 701,
            'schedule_fingerprint': 'verified-best',
            'operation_count': 299,
        })
        self.assertNotIn('neighborhoods', compact)

    def test_completed_summary_retains_bounded_production_telemetry(self):
        telemetry = {
            'schema_version': 1,
            'neighborhood_count': 12,
            'phase_seconds': {'kernel_seconds': 4.5},
        }

        compact = _compact_search_summary({
            'production_telemetry': telemetry,
            'neighborhoods': [{'large': 'diagnostic payload'}],
        })

        self.assertEqual(compact['production_telemetry'], telemetry)
        self.assertNotIn('neighborhoods', compact)


class ProductionOptimizerTelemetryTests(SimpleTestCase):
    def test_aggregates_only_neighborhood_boundary_metrics(self):
        telemetry = ProductionOptimizerTelemetry()
        transition = {
            'operation': 'rotate',
            'authoritative_official_delta': -2500,
        }

        telemetry.record_neighborhood(
            kernel_result={
                'engine_context_cache_hit': True,
                'preparation_seconds': 1.0,
                'elapsed_seconds': 4.0,
                'authoritative_validation': {'elapsed_seconds': 2.0},
                'in_process_transition': {'refresh_seconds': 0.5},
            },
            selection_mode='improve',
            proposed_transition=transition,
            accepted_transition=transition,
            structural_attempt={
                'selected_family': 'weekend',
                'accepted': True,
                'score_delta': -2500,
            },
            command_seconds=8.0,
        )
        telemetry.record_verified_checkpoint(
            elapsed_seconds=8.0,
            accepted_transitions=1,
            score=7500,
        )
        telemetry.record_verified_checkpoint(
            elapsed_seconds=14.0,
            accepted_transitions=2,
            score=7000,
        )

        summary = telemetry.summary(
            wall_seconds=20.0,
            total_evaluations=2_000_000,
            stopped_reason='runtime_limit',
        )

        self.assertEqual(summary['neighborhood_count'], 1)
        self.assertEqual(summary['engine_context_cache'], {
            'hits': 1, 'misses': 0,
        })
        self.assertEqual(summary['phase_seconds'], {
            'authoritative_validation_seconds': 2.0,
            'command_overhead_seconds': 1.0,
            'command_seconds': 8.0,
            'kernel_seconds': 4.0,
            'preparation_seconds': 1.0,
            'transition_refresh_seconds': 0.5,
        })
        self.assertEqual(
            summary['operations']['rotate']['official_penalty_reduction'],
            2500.0,
        )
        self.assertEqual(summary['structural_families']['weekend']['accepted'], 1)
        self.assertEqual(summary['first_verified_checkpoint_seconds'], 8.0)
        self.assertEqual(summary['best_verified_score'], 7000.0)
        self.assertEqual(summary['best_verified_seconds'], 14.0)
        self.assertEqual(summary['end_to_end_evaluations_per_second'], 100000.0)

    def test_failed_structural_attempt_does_not_claim_vector_acceptance(self):
        telemetry = ProductionOptimizerTelemetry()
        vector_transition = {
            'operation': 'swap',
            'authoritative_official_delta': -100,
        }

        telemetry.record_neighborhood(
            kernel_result={},
            selection_mode='diversify',
            proposed_transition=vector_transition,
            accepted_transition=vector_transition,
            structural_attempt={
                'selected_family': 'night',
                'accepted': False,
                'score_delta': None,
            },
            command_seconds=1.0,
        )
        summary = telemetry.summary(
            wall_seconds=1.0,
            total_evaluations=10,
            stopped_reason='runtime_limit',
        )

        self.assertEqual(summary['operations']['swap']['accepted'], 1)
        self.assertEqual(summary['structural_families']['night']['attempts'], 1)
        self.assertEqual(summary['structural_families']['night']['accepted'], 0)

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

    def test_boundary_counts_keep_overlap_and_rest_components_separate(self):
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

        overlap_counts, rest_counts = (
            published_boundary_conflict_count_matrices(
                physician_ids=[101],
                shift_starts=np.asarray([
                    self._timestamp(5, 30),
                    self._timestamp(10),
                    self._timestamp(16),
                ]),
                shift_ends=np.asarray([
                    self._timestamp(14),
                    self._timestamp(18),
                    datetime(
                        2027, 2, 2, 0, 0, tzinfo=timezone.utc,
                    ).timestamp(),
                ]),
                boundary_context=boundary_context,
                minimum_rest_seconds=np.asarray([10 * 3600]),
            )
        )

        np.testing.assert_array_equal(overlap_counts, np.asarray([[1, 0, 0]]))
        np.testing.assert_array_equal(rest_counts, np.asarray([[0, 1, 0]]))

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
            eligible_shift=np.asarray([
                [True, True],
                [True, True],
                [False, False],
                [True, False],
            ]),
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

    def test_context_tracks_revisited_state_and_rejects_invalid_count(self):
        with self.assertRaises(ValueError):
            self.context.record_neighborhood(-1)
        cycled = V2EngineContext(
            state=self.context.state,
            assignment_rows=self.context.assignment_rows,
            seen_fingerprints=self.context.seen_fingerprints | frozenset((
                schedule_fingerprint({10: [2], 20: [1], 30: [3]}),
            )),
        )
        revisited = cycled.apply_selection(
            self.selection,
            [(10, 2), (20, 1), (30, 3)],
            [(10, 2), (20, 1)],
            {
                'workload': np.asarray([[100], [200]]),
                'weekend': np.asarray([[1000], [2000]]),
            },
        )
        self.assertEqual(revisited.state[10], (2,))
        self.assertEqual(revisited.state[20], (1,))
        self.assertEqual(revisited.accepted_transitions, 1)
        self.assertEqual(
            revisited.seen_fingerprints,
            cycled.seen_fingerprints,
        )
