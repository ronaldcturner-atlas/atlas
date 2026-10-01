import io
import json
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.scheduling.management.commands.benchmark_v2_continuous import Command


class V2ContinuousCommandTests(SimpleTestCase):
    @staticmethod
    def _kernel_result(fingerprint, transition, current_score=None):
        return {
            'run_number': 95,
            'schedule_fingerprint': fingerprint,
            'distinct_candidate_schedules': 1,
            'elapsed_seconds': 0.1,
            'preparation_seconds': 0.1,
            'engine_context_cache_hit': True,
            'distinct_schedules_per_second': 10.0,
            'meets_stage_one_rate': True,
            'best_candidate_transition_validation': transition,
            'authoritative_current_score': current_score,
        }

    @staticmethod
    def _transition(index):
        return {
            'left_instance_id': index * 2 + 1,
            'left_physician_id': 10,
            'right_instance_id': index * 2 + 2,
            'right_physician_id': 20,
            'authoritative_legal': True,
            'authoritative_improving': True,
            'authoritative_official_delta': -10.0,
            'predicted_official_delta': -10.0,
        }

    @staticmethod
    def _diversification_transition(index, *, penalty_delta=0.0):
        return {
            'left_instance_id': index * 2 + 1,
            'left_physician_id': 10,
            'right_instance_id': index * 2 + 2,
            'right_physician_id': 20,
            'authoritative_legal': True,
            'authoritative_improving': False,
            'accepted_for_diversification': True,
            'authoritative_official_delta': penalty_delta,
            'predicted_official_delta': penalty_delta,
            'predicted_proportionality_delta': 1.0,
        }

    @staticmethod
    def _structural_patch_transition(*, penalty_delta=-10.0):
        return {
            'operation': 'patch',
            'source': 'structural_deep_reconstruction',
            'reassignments': [[101, 10, 20], [102, 20, 10]],
            'authoritative_legal': True,
            'authoritative_improving': penalty_delta < 0,
            'accepted_for_diversification': True,
            'authoritative_official_delta': penalty_delta,
            'predicted_official_delta': penalty_delta,
            'predicted_proportionality_delta': 0.0,
        }

    @classmethod
    def _unproductive_generation(
        cls, prefix, start_index, current_score=100.0, *, include_initial=True,
    ):
        rows = [
            cls._kernel_result(
                f'{prefix}-soft-kick',
                cls._diversification_transition(start_index),
                current_score,
            ),
            cls._kernel_result(f'{prefix}-soft-local-optimum', None),
            cls._kernel_result(
                f'{prefix}-first-deep-kick',
                cls._diversification_transition(start_index + 1),
                current_score,
            ),
            cls._kernel_result(f'{prefix}-first-deep-local-optimum', None),
            cls._kernel_result(
                f'{prefix}-second-deep-kick',
                cls._diversification_transition(start_index + 2),
                current_score,
            ),
            cls._kernel_result(f'{prefix}-second-deep-local-optimum', None),
        ]
        if include_initial:
            rows.insert(0, cls._kernel_result(
                f'{prefix}-initial-local-optimum', None, current_score,
            ))
        return rows

    def test_command_exposes_read_only_continuous_options(self):
        parser = Command().create_parser('manage.py', 'benchmark_v2_continuous')
        options = parser.parse_args(['--run-number', '91'])

        self.assertEqual(options.run_number, 91)
        self.assertEqual(options.target_evaluations, 100_000_000)
        self.assertEqual(options.max_transitions, 100)
        self.assertIsNone(options.max_runtime_seconds)
        self.assertIsNone(options.configured_runtime_seconds)
        self.assertEqual(options.minimum_rate, 600_000.0)
        self.assertEqual(options.stress_contract_count, 0)
        self.assertEqual(options.validate_sample, 1)
        self.assertEqual(options.checkpoint_interval, 20)
        self.assertEqual(options.soft_restart_moves, 3)
        self.assertEqual(options.deep_restart_moves, 8)
        self.assertFalse(options.distribution_focus)
        self.assertEqual(options.initial_swap, [])

    def test_command_accepts_ordered_resume_swaps(self):
        parser = Command().create_parser('manage.py', 'benchmark_v2_continuous')
        options = parser.parse_args([
            '--run-number', '91',
            '--initial-swap', '10:1:20:2',
            '--initial-swap', '30:3:40:4',
        ])

        self.assertEqual(
            options.initial_swap,
            ['10:1:20:2', '30:3:40:4'],
        )

    def test_continuous_search_checkpoints_compiled_score_periodically(self):
        checkpoint_flags = []
        results = [
            self._kernel_result('a', self._transition(0)),
            self._kernel_result('b', self._transition(1)),
            self._kernel_result('c', None, current_score=80.0),
        ]

        def fake_kernel(_name, **kwargs):
            checkpoint_flags.append(kwargs['checkpoint_score'])
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=95, run_id=None, target_evaluations=100,
                max_transitions=2, max_runtime_seconds=None,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=2,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=3,
                deep_restart_moves=8,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(checkpoint_flags, [False, False, True])
        self.assertEqual(result['predicted_final_score'], 80.0)
        self.assertEqual(
            result['score_checkpoints'],
            [{
                'accepted_transitions': 2,
                'score': 80.0,
                'epoch': 1,
                'restart_mode': 'initial',
                'selection_mode': 'improve',
            }],
        )

    def test_continuous_search_rejects_checkpoint_score_divergence(self):
        results = [
            self._kernel_result('a', self._transition(0)),
            self._kernel_result('b', None, current_score=89.0),
        ]

        def fake_kernel(_name, **kwargs):
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            with self.assertRaisesMessage(
                CommandError, 'diverged from the authoritative checkpoint',
            ):
                Command().handle(
                    run_number=95, run_id=None, target_evaluations=100,
                    max_transitions=2, max_runtime_seconds=None,
                    minimum_rate=0, stress_contract_count=0,
                    validate_sample=0, checkpoint_interval=1,
                    initial_swap=[], as_json=True, stop_requested=None,
                    progress_callback=None, starting_score=100,
                    distribution_focus=False, soft_restart_moves=3,
                    deep_restart_moves=8,
                    soft_restart_maximum_penalty=5000,
                    deep_restart_maximum_penalty=10000,
                )

    def test_zero_gain_search_requires_two_unproductive_generations(self):
        results = [
            self._kernel_result('initial', None),
            self._kernel_result(
                'soft-kick', self._diversification_transition(1), 100.0,
            ),
            self._kernel_result('soft-local-optimum', None),
            self._kernel_result(
                'deep-kick', self._diversification_transition(2), 100.0,
            ),
            self._kernel_result('first-deep-local-optimum', None),
            self._kernel_result(
                'second-deep-kick', self._diversification_transition(3), 100.0,
            ),
            self._kernel_result('second-deep-local-optimum', None),
        ] + self._unproductive_generation('generation-two', 4)

        def fake_kernel(_name, **kwargs):
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=95, run_id=None, target_evaluations=100,
                max_transitions=20, max_runtime_seconds=None,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=20,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=1,
                deep_restart_moves=1,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(result['stopped_reason'], 'productivity_exhausted')
        self.assertEqual(
            [row['restart_mode'] for row in result['restart_details']],
            ['soft', 'deep', 'deep', 'soft', 'deep', 'deep'],
        )
        self.assertEqual(
            [row['productive'] for row in result['pipeline_epochs']],
            [False, False, False, False, False, False, False, False],
        )
        self.assertEqual(
            [row['productive'] for row in result['generation_details']],
            [False, False],
        )
        self.assertEqual(result['predicted_final_score'], 100.0)
        self.assertEqual(result['swaps'], [])

    def test_productive_soft_epoch_resets_exhaustion_before_next_stall(self):
        results = [
            self._kernel_result('initial', None),
            self._kernel_result(
                'first-soft-kick', self._diversification_transition(1), 100.0,
            ),
            self._kernel_result('first-soft-improvement', self._transition(2)),
            self._kernel_result('first-soft-local-optimum', None),
            self._kernel_result(
                'second-soft-kick', self._diversification_transition(3), 90.0,
            ),
            self._kernel_result('second-soft-local-optimum', None),
            self._kernel_result(
                'third-soft-kick', self._diversification_transition(4), 90.0,
            ),
            self._kernel_result('third-soft-local-optimum', None),
            self._kernel_result(
                'deep-kick', self._diversification_transition(5), 90.0,
            ),
            self._kernel_result('first-deep-local-optimum', None),
            self._kernel_result(
                'second-deep-kick', self._diversification_transition(6), 90.0,
            ),
            self._kernel_result('second-deep-local-optimum', None),
        ] + self._unproductive_generation(
            'generation-two', 7, 90.0,
        ) + self._unproductive_generation('generation-three', 10, 90.0)

        def fake_kernel(_name, **kwargs):
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=95, run_id=None, target_evaluations=100,
                max_transitions=30, max_runtime_seconds=None,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=20,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=1,
                deep_restart_moves=1,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(result['stopped_reason'], 'productivity_exhausted')
        self.assertEqual(result['predicted_final_score'], 90.0)
        self.assertEqual(
            [row['restart_mode'] for row in result['restart_details']],
            [
                'soft', 'soft', 'soft', 'deep', 'deep',
                'soft', 'deep', 'deep',
                'soft', 'deep', 'deep',
            ],
        )
        self.assertEqual(
            [row['productive'] for row in result['pipeline_epochs']],
            [
                False, True, False, False, False, False,
                False, False, False, False,
                False, False, False, False,
            ],
        )
        self.assertEqual(
            [row['productive'] for row in result['generation_details']],
            [True, False, False],
        )

    def test_restart_without_diversification_candidate_exhausts_cleanly(self):
        results = [
            self._kernel_result('initial', None),
            self._kernel_result('soft-no-candidate', None, 100.0),
            self._kernel_result('first-deep-no-candidate', None, 100.0),
            self._kernel_result('second-deep-no-candidate', None, 100.0),
            self._kernel_result('generation-two-soft-no-candidate', None, 100.0),
            self._kernel_result('generation-two-deep-no-candidate', None, 100.0),
            self._kernel_result(
                'generation-two-second-deep-no-candidate', None, 100.0,
            ),
        ]
        results.insert(4, self._kernel_result(
            'generation-two-initial-no-candidate', None, 100.0,
        ))
        selection_modes = []
        reset_engine_context_flags = []
        deep_restart_numbers = []

        def fake_kernel(_name, **kwargs):
            selection_modes.append(kwargs['selection_mode'])
            reset_engine_context_flags.append(kwargs['reset_engine_context'])
            deep_restart_numbers.append(kwargs['deep_restart_number'])
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=98, run_id=None, target_evaluations=100,
                max_transitions=10, max_runtime_seconds=None,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=20,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=1,
                deep_restart_moves=1,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(
            selection_modes,
            [
                'improve',
                'diversify', 'diversify', 'diversify',
                'improve', 'diversify', 'diversify', 'diversify',
            ],
        )
        self.assertEqual(result['stopped_reason'], 'productivity_exhausted')
        self.assertEqual(result['accepted_transitions'], 0)
        self.assertEqual(result['predicted_final_score'], 100.0)
        self.assertEqual(result['swaps'], [])
        self.assertEqual(
            reset_engine_context_flags,
            [False, False, False, False, True, False, False, False],
        )
        self.assertEqual(
            deep_restart_numbers,
            [0, 0, 1, 2, 0, 0, 1, 2],
        )

    def test_generation_samples_each_available_structural_family(self):
        results = [
            self._kernel_result('initial', None, 100.0),
            self._kernel_result('soft', None, 100.0),
            self._kernel_result('deep-night', None, 100.0),
            self._kernel_result('deep-weekend', None, 100.0),
        ]
        results[2]['structural_reconstruction_attempt'] = {
            'selected_family': 'night',
            'available_families': ['weekend', 'night'],
        }
        results[3]['structural_reconstruction_attempt'] = {
            'selected_family': 'weekend',
            'available_families': ['weekend', 'night'],
        }
        excluded_families = []

        def fake_kernel(_name, **kwargs):
            excluded_families.append(
                kwargs['exclude_structural_focus_family']
            )
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=105, run_id=None, target_evaluations=100,
                max_transitions=3, max_runtime_seconds=None,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=20,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=1,
                deep_restart_moves=1,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(excluded_families[2], [])
        self.assertEqual(excluded_families[3], ['night'])
        self.assertEqual(result['search_generation'], 2)
        self.assertEqual(
            result['generation_details'][0]['productive'], False,
        )

    def test_first_deep_move_requests_and_applies_structural_reconstruction(self):
        results = [
            self._kernel_result('initial', None),
            self._kernel_result('soft-no-candidate', None, 100.0),
            self._kernel_result(
                'deep-structural-patch',
                self._structural_patch_transition(),
                100.0,
            ),
        ]
        structural_flags = []
        deep_restart_numbers = []

        def fake_kernel(_name, **kwargs):
            structural_flags.append(kwargs['structural_reconstruction'])
            deep_restart_numbers.append(kwargs['deep_restart_number'])
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=100, run_id=None, target_evaluations=100,
                max_transitions=2, max_runtime_seconds=None,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=20,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=1,
                deep_restart_moves=1,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(structural_flags, [False, False, True])
        self.assertEqual(deep_restart_numbers, [0, 0, 1])
        self.assertEqual(result['predicted_final_score'], 90.0)
        self.assertEqual(
            result['swaps'],
            ['R:101:10:20', 'R:102:20:10'],
        )

    def test_unproductive_generation_resets_before_runtime_halfway_point(self):
        results = [self._kernel_result('initial', None)] + (
            self._unproductive_generation(
                'generation-one', 1, include_initial=False,
            )
            + self._unproductive_generation('generation-two', 4)
        )

        elapsed = [0.0]

        def fake_kernel(_name, **kwargs):
            result = results.pop(0)
            if result['schedule_fingerprint'] == (
                'generation-two-second-deep-local-optimum'
            ):
                elapsed[0] = 3601.0
            kwargs['stdout'].write(json.dumps(result))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=100, run_id=None, target_evaluations=100,
                max_transitions=20, max_runtime_seconds=7200,
                configured_runtime_seconds=7200,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=20,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=1,
                deep_restart_moves=1,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
                runtime_clock=lambda: elapsed[0],
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(result['stopped_reason'], 'productivity_exhausted')
        self.assertEqual(
            [row['restart_mode'] for row in result['restart_details']],
            ['soft', 'deep', 'deep', 'soft', 'deep', 'deep'],
        )
        self.assertEqual(result['search_generation'], 2)
        self.assertEqual(
            [row['productive'] for row in result['generation_details']],
            [False, False],
        )
        self.assertEqual(result['configured_runtime_seconds'], 7200.0)

    def test_two_unproductive_generations_stop_after_halfway_point(self):
        results = [self._kernel_result('initial', None)] + (
            self._unproductive_generation(
                'generation-one', 1, include_initial=False,
            )
            + self._unproductive_generation('generation-two', 4)
        )

        def fake_kernel(_name, **kwargs):
            kwargs['stdout'].write(json.dumps(results.pop(0)))

        command = Command()
        command.stdout = io.StringIO()
        with patch(
            'apps.scheduling.management.commands.'
            'benchmark_v2_continuous.call_command',
            side_effect=fake_kernel,
        ):
            command.handle(
                run_number=100, run_id=None, target_evaluations=100,
                max_transitions=20, max_runtime_seconds=3000,
                configured_runtime_seconds=7200,
                minimum_rate=0, stress_contract_count=0,
                validate_sample=0, checkpoint_interval=20,
                initial_swap=[], as_json=True, stop_requested=None,
                progress_callback=None, starting_score=100,
                distribution_focus=False, soft_restart_moves=1,
                deep_restart_moves=1,
                soft_restart_maximum_penalty=5000,
                deep_restart_maximum_penalty=10000,
            )

        result = json.loads(command.stdout.getvalue())
        self.assertEqual(
            [row['restart_mode'] for row in result['restart_details']],
            ['soft', 'deep', 'deep', 'soft', 'deep', 'deep'],
        )
        self.assertEqual(result['search_generation'], 2)
        self.assertEqual(
            [row['productive'] for row in result['generation_details']],
            [False, False],
        )
