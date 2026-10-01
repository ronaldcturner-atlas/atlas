from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.scheduling.initial_schedule import (
    construct_complete_initial_schedule,
)
from apps.scheduling.models import OptimizerRun
from apps.scheduling.optimizer import optimize_schedule_version


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
            'apps.scheduling.initial_schedule.optimize_schedule_version',
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
                'seed': 9183,
                'start_mode': OptimizerRun.StartMode.FRESH_FILL,
                'source_run': None,
                'max_runtime_seconds': 7200,
                'optimization_focus': (
                    OptimizerRun.OptimizationFocus.STANDARD
                ),
                'adaptive_runtime': True,
                'isolated_run': True,
                'finalize_run': False,
                'construction_only': True,
            },
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
            'apps.scheduling.initial_schedule.optimize_schedule_version',
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
                'apps.scheduling.initial_schedule.optimize_schedule_version',
                return_value=summary,
            ):
                with self.assertRaisesMessage(ValueError, message):
                    construct_complete_initial_schedule(
                        'schedule-version', optimizer_run=self._run(),
                    )
