from datetime import date, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.domains.models import Domain
from .management.commands.benchmark_optimizer import summarize_best_chain, summarize_trials
from .models import OptimizerRun, ScheduleBlock, ScheduleVersion
from .optimizer import _state_from_assignments


class OptimizerBenchmarkCommandTests(TestCase):
    def setUp(self):
        self.domain = Domain.objects.create(name='Physician', active=True)
        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            request_open_datetime=timezone.make_aware(datetime(2026, 6, 1, 8)),
            request_close_datetime=timezone.make_aware(datetime(2026, 6, 2, 8)),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        self.version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Benchmark fixture',
            status=ScheduleVersion.Status.BUILD,
        )

    def test_command_runs_once_prints_summary_and_rolls_back(self):
        stdout = StringIO()

        call_command(
            'benchmark_optimizer',
            schedule_block_id=self.block.id,
            domain='Physician',
            runs=1,
            stdout=stdout,
        )

        output = stdout.getvalue()
        for field in (
            'attempted_runs', 'completed_runs', 'failed_runs', 'best_final_score',
            'median_final_score', 'worst_final_score', 'average_runtime_seconds',
            'best_seed', 'score_spread', 'any_run_improved_from_initial',
        ):
            self.assertIn(field, output)
        self.assertFalse(OptimizerRun.objects.filter(schedule_version=self.version).exists())

    def test_preview_block_is_allowed_and_remains_preview(self):
        self.block.build_status = ScheduleBlock.BuildStatus.PREVIEW
        self.block.save(update_fields=['build_status'])
        existing_run = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            seed=99,
            run_kind='OPTIMIZER',
            is_active=True,
        )
        stdout = StringIO()

        call_command(
            'benchmark_optimizer',
            schedule_block_id=self.block.id,
            domain='Physician',
            runs=1,
            stdout=stdout,
        )

        self.block.refresh_from_db()
        existing_run.refresh_from_db()
        output = stdout.getvalue()
        self.assertEqual(self.block.build_status, ScheduleBlock.BuildStatus.PREVIEW)
        self.assertTrue(existing_run.is_active)
        self.assertEqual(
            list(OptimizerRun.objects.filter(schedule_version=self.version).values_list('id', flat=True)),
            [existing_run.id],
        )
        self.assertIn('Schedule block status before benchmark: PREVIEW', output)
        self.assertIn('Schedule block status after benchmark: PREVIEW', output)
        self.assertIn('kind=BENCHMARK', output)

    def test_best_chain_accepts_only_improvements_and_preserves_source(self):
        self.block.build_status = ScheduleBlock.BuildStatus.PREVIEW
        self.block.save(update_fields=['build_status'])
        source = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            seed=99,
            final_score=100,
            score_breakdown={'coverage': 100},
            optimizer_summary={'marker': 'unchanged'},
            run_kind='OPTIMIZER',
            is_active=True,
        )
        source_snapshot = {
            'final_score': source.final_score,
            'score_breakdown': source.score_breakdown,
            'optimizer_summary': source.optimizer_summary,
            'run_kind': source.run_kind,
            'is_active': source.is_active,
        }
        candidate_scores = iter((120, 80, 90))
        source_ids = []
        candidate_ids = []

        def fake_optimize(version, **kwargs):
            source_ids.append(kwargs['source_run'].id)
            score = next(candidate_scores)
            candidate = OptimizerRun.objects.create(
                schedule_version=version,
                run_number=version.optimizer_runs.count() + 1,
                status=OptimizerRun.Status.COMPLETED,
                seed=kwargs['seed'],
                initial_score=kwargs['source_run'].final_score,
                final_score=score,
                run_kind=kwargs['run_kind'],
            )
            candidate_ids.append(candidate.id)
            return {
                'optimizer_run_id': candidate.id,
                'initial_score': float(kwargs['source_run'].final_score),
                'final_score': score,
                'runtime_seconds': 0.01,
                'stopped_reason': 'completed',
                'timed_out': False,
                'debug': {
                    'assignments_same_at_start': True,
                    'copied_start_assignment_count': 0,
                    'assignments_changed_before_first_score': 0,
                    'source_state_reported_score_before_pre_score_changes': float(
                        kwargs['source_run'].final_score
                    ),
                    'score_audit': {'total_score': score},
                },
            }

        stdout = StringIO()
        snapshot_scores = iter((100, 100, 120, 100, 80, 80, 90))
        with patch(
            'apps.scheduling.management.commands.benchmark_optimizer.optimizer.optimize_schedule_version',
            side_effect=fake_optimize,
        ), patch(
            'apps.scheduling.management.commands.benchmark_optimizer.Command._source_snapshot',
            side_effect=lambda _version, _run: {
                'score': next(snapshot_scores),
                'assignment_count': 0,
                'assignment_ids': [],
                'shift_instance_ids': [],
            },
        ):
            call_command(
                'benchmark_optimizer',
                schedule_block_id=self.block.id,
                domain='Physician',
                runs=3,
                mode='best-chain',
                stdout=stdout,
            )

        self.block.refresh_from_db()
        source.refresh_from_db()
        output = stdout.getvalue()
        self.assertEqual(source_ids, [source.id, source.id, candidate_ids[1]])
        self.assertIn('candidate_score=120.0', output)
        self.assertIn('candidate_score=80.0', output)
        self.assertIn('candidate_score=90.0', output)
        self.assertIn('optimizer_initial_score=100.0', output)
        self.assertIn('initial_matches_source=yes', output)
        self.assertIn('assignments_same_at_start=yes', output)
        self.assertIn('source_assignments_copied=0', output)
        self.assertIn('assignments_changed_before_first_score=0', output)
        self.assertIn('final_matches_score_audit=yes', output)
        self.assertEqual(output.count('accepted_as_new_best=yes'), 1)
        self.assertIn('best_score_so_far=100', output)
        self.assertEqual(output.count('best_score_so_far=80.0'), 2)
        self.assertIn('accepted_bests_monotonically_improved: True', output)
        self.assertEqual(self.block.build_status, ScheduleBlock.BuildStatus.PREVIEW)
        self.assertEqual(
            {
                'final_score': source.final_score,
                'score_breakdown': source.score_breakdown,
                'optimizer_summary': source.optimizer_summary,
                'run_kind': source.run_kind,
                'is_active': source.is_active,
            },
            source_snapshot,
        )
        self.assertEqual(
            list(OptimizerRun.objects.filter(schedule_version=self.version).values_list('id', flat=True)),
            [source.id],
        )

    def test_best_chain_aborts_when_source_and_optimizer_initial_scores_differ(self):
        source = OptimizerRun.objects.create(
            schedule_version=self.version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            final_score=100,
            run_kind='OPTIMIZER',
            is_active=True,
        )

        def fake_optimize(version, **kwargs):
            candidate = OptimizerRun.objects.create(
                schedule_version=version,
                run_number=2,
                status=OptimizerRun.Status.COMPLETED,
                initial_score=120,
                final_score=90,
                run_kind='BENCHMARK',
            )
            return {
                'optimizer_run_id': candidate.id,
                'initial_score': 120,
                'final_score': 90,
                'debug': {
                    'assignments_same_at_start': True,
                    'copied_start_assignment_count': 0,
                    'source_pairs_missing_at_start': [],
                    'source_pairs_extra_at_start': [],
                },
            }

        snapshot = {
            'score': 100,
            'assignment_count': 0,
            'assignment_ids': [],
            'shift_instance_ids': [],
        }
        with patch(
            'apps.scheduling.management.commands.benchmark_optimizer.optimizer.optimize_schedule_version',
            side_effect=fake_optimize,
        ), patch(
            'apps.scheduling.management.commands.benchmark_optimizer.Command._source_snapshot',
            return_value=snapshot,
        ):
            with self.assertRaisesMessage(
                CommandError,
                'source_recomputed_score=100',
            ):
                call_command(
                    'benchmark_optimizer',
                    schedule_block_id=self.block.id,
                    domain='Physician',
                    runs=1,
                    mode='best-chain',
                )

        source.refresh_from_db()
        self.assertTrue(source.is_active)
        self.assertEqual(source.final_score, 100)

    @patch(
        'apps.scheduling.management.commands.benchmark_optimizer.optimizer.optimize_schedule_version',
        side_effect=RuntimeError('synthetic failure'),
    )
    def test_failed_run_does_not_crash_benchmark(self, optimize_mock):
        stdout = StringIO()

        call_command(
            'benchmark_optimizer',
            schedule_block_id=self.block.id,
            domain='Physician',
            runs=2,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn('failed_runs: 2', output)
        self.assertIn('status=ERROR', output)
        self.assertEqual(optimize_mock.call_count, 2)


class OptimizerBenchmarkSummaryTests(TestCase):
    def test_summary_uses_completed_runs_only(self):
        result = summarize_trials([
            {'status': 'COMPLETED', 'seed': 7, 'initial_score': 20, 'final_total_score': 10, 'runtime_seconds': 3},
            {'status': 'COMPLETED', 'seed': 8, 'initial_score': 20, 'final_total_score': 20, 'runtime_seconds': 1},
            {'status': 'ERROR', 'seed': 9, 'initial_score': None, 'final_total_score': None, 'runtime_seconds': 1},
        ])

        self.assertEqual(result['attempted_runs'], 3)
        self.assertEqual(result['completed_runs'], 2)
        self.assertEqual(result['failed_runs'], 1)
        self.assertEqual(result['best_final_score'], 10)
        self.assertEqual(result['median_final_score'], 15)
        self.assertEqual(result['worst_final_score'], 20)
        self.assertEqual(result['average_runtime_seconds'], 2)
        self.assertEqual(result['best_seed'], 7)
        self.assertEqual(result['score_spread'], 10)
        self.assertTrue(result['any_run_improved_from_initial'])

    def test_best_chain_summary_never_increases_accepted_bests(self):
        result = summarize_best_chain(
            100,
            70,
            4,
            [
                {'accepted_as_new_best': False, 'best_score_so_far': 100},
                {'accepted_as_new_best': True, 'best_score_so_far': 80},
                {'accepted_as_new_best': True, 'best_score_so_far': 70},
            ],
            3.5,
        )

        self.assertEqual(result['absolute_improvement'], 30)
        self.assertEqual(result['percent_improvement'], 30)
        self.assertEqual(result['accepted_improvements'], 2)
        self.assertTrue(result['accepted_bests_monotonically_improved'])

    def test_schedule_state_deduplicates_identical_assignment_pairs(self):
        assignment = SimpleNamespace(
            shift_instance_id=7,
            physician_id=11,
            assignment_source='OPTIMIZER',
            is_locked=False,
        )

        state, _manual_pairs = _state_from_assignments([assignment, assignment])

        self.assertEqual(state[7], [11])
