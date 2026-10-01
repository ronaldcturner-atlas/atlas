from types import SimpleNamespace
from unittest.mock import Mock, patch
from io import StringIO

from django.test import SimpleTestCase
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.scheduling.initial_schedule import (
    construct_complete_initial_schedule,
)
from apps.scheduling.management.commands.verify_initial_schedule_equivalence import (
    _snapshot_differences,
    _snapshot_hash,
)
from apps.scheduling.models import OptimizerRun
from apps.scheduling.optimizer import optimize_schedule_version
from apps.scheduling.optimizer_v1_constructor import (
    construct_complete_fresh_fill_schedule,
)


class InitialScheduleConstructorTests(SimpleTestCase):
    @staticmethod
    def _run():
        return SimpleNamespace(
            seed=9183,
            max_runtime_seconds=7200,
            optimization_focus=OptimizerRun.OptimizationFocus.STANDARD,
            refresh_from_db=Mock(),
        )

    def test_constructor_preserves_proven_fresh_fill_invocation(self):
        run = self._run()
        forwarded_progress = Mock()
        def proven_constructor(_version, **kwargs):
            kwargs['progress_callback'](2923000, force=True)
            return {
                'unfilled_shift_count': 0,
                'final_overlap_violations': 0,
                'final_score': 2923000,
            }

        with patch(
            'apps.scheduling.initial_schedule.construct_complete_fresh_fill_schedule',
            side_effect=proven_constructor,
        ) as constructor:
            summary = construct_complete_initial_schedule(
                'schedule-version',
                optimizer_run=run,
                created_by='scheduler',
                stop_requested=lambda: False,
                progress_callback=forwarded_progress,
            )

        self.assertEqual(summary['final_score'], 2923000)
        forwarded_progress.assert_called_once_with(2923000, force=True)
        run.refresh_from_db.assert_called_once_with()
        self.assertEqual(
            constructor.call_args.args,
            ('schedule-version',),
        )
        self.assertEqual(
            {
                key: value
                for key, value in constructor.call_args.kwargs.items()
                if key not in {'stop_requested', 'progress_callback'}
            },
            {
                'created_by': 'scheduler',
                'optimizer_run': run,
            },
        )

    def test_proven_constructor_adapter_pins_safe_fresh_fill_options(self):
        run = self._run()
        stopped = lambda: False
        progress = Mock()
        expected = {'final_score': 2923000}

        with patch(
            'apps.scheduling.optimizer.optimize_schedule_version',
            return_value=expected,
        ) as optimizer:
            result = construct_complete_fresh_fill_schedule(
                'schedule-version',
                optimizer_run=run,
                created_by='scheduler',
                stop_requested=stopped,
                progress_callback=progress,
            )

        self.assertIs(result, expected)
        optimizer.assert_called_once_with(
            'schedule-version',
            created_by='scheduler',
            optimizer_run=run,
            seed=9183,
            start_mode=OptimizerRun.StartMode.FRESH_FILL,
            source_run=None,
            max_runtime_seconds=7200,
            optimization_focus=OptimizerRun.OptimizationFocus.STANDARD,
            adaptive_runtime=True,
            stop_requested=stopped,
            progress_callback=progress,
            isolated_run=True,
            finalize_run=False,
            construction_only=True,
        )

    def test_user_stop_is_visible_before_construction_completes(self):
        run = self._run()

        def proven_constructor(_version, **kwargs):
            self.assertTrue(kwargs['stop_requested']())
            return {
                'unfilled_shift_count': 0,
                'final_overlap_violations': 0,
            }

        with patch(
            'apps.scheduling.initial_schedule.construct_complete_fresh_fill_schedule',
            side_effect=proven_constructor,
        ):
            construct_complete_initial_schedule(
                'schedule-version',
                optimizer_run=run,
                stop_requested=lambda: True,
            )

    def test_construction_only_mode_rejects_unsafe_invocations(self):
        with self.assertRaisesMessage(ValueError, 'requires a Fresh Fill'):
            optimize_schedule_version(
                None,
                start_mode=OptimizerRun.StartMode.CURRENT_SCHEDULE,
                construction_only=True,
                finalize_run=False,
            )
        with self.assertRaisesMessage(ValueError, 'must leave the optimizer'):
            optimize_schedule_version(
                None,
                start_mode=OptimizerRun.StartMode.FRESH_FILL,
                construction_only=True,
                finalize_run=True,
            )

    def test_incomplete_or_overlapping_construction_is_rejected(self):
        cases = (
            (
                {'unfilled_shift_count': 1, 'final_overlap_violations': 0},
                'complete starting schedule',
            ),
            (
                {'unfilled_shift_count': 0, 'final_overlap_violations': 1},
                'time-overlap conflict',
            ),
        )
        for summary, message in cases:
            with self.subTest(summary=summary), patch(
                'apps.scheduling.initial_schedule.construct_complete_fresh_fill_schedule',
                return_value=summary,
            ):
                with self.assertRaisesMessage(ValueError, message):
                    construct_complete_initial_schedule(
                        'schedule-version', optimizer_run=self._run(),
                    )

    def test_equivalence_snapshot_detects_assignment_and_score_drift(self):
        baseline = {
            'assignments': [(1, 10, 'OPTIMIZER', False)],
            'final_score': 100,
            'score_breakdown': {'request_score': 100},
            'validity': {'unfilled_shift_count': 0},
        }
        self.assertEqual(_snapshot_differences(baseline, dict(baseline)), [])
        self.assertEqual(_snapshot_hash(baseline), _snapshot_hash(dict(baseline)))

        changed = {
            **baseline,
            'assignments': [(1, 11, 'OPTIMIZER', False)],
            'final_score': 200,
        }
        self.assertEqual(
            [row['field'] for row in _snapshot_differences(baseline, changed)],
            ['assignments', 'final_score'],
        )
        self.assertNotEqual(_snapshot_hash(baseline), _snapshot_hash(changed))

    @patch(
        'apps.scheduling.management.commands.'
        'verify_initial_schedule_equivalence.OptimizerRun.objects.filter'
    )
    def test_equivalence_command_rejects_active_optimizer(self, filter_runs):
        filter_runs.return_value.exists.return_value = True
        with self.assertRaisesMessage(CommandError, 'slots to be idle'):
            call_command(
                'verify_initial_schedule_equivalence',
                1,
                expected_hash='reference',
                stdout=StringIO(),
            )
