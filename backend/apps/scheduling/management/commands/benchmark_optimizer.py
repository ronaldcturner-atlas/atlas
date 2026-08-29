import csv
import json
import secrets
from contextlib import nullcontext
from pathlib import Path
from statistics import mean, median
from time import monotonic
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.scheduling import optimizer
from apps.scheduling.models import (
    OptimizerRun, ScheduleShiftAssignment, ScheduleShiftInstance, ScheduleVersion,
)
from apps.scheduling.run_state import assignments_for_viewed_run


TRIAL_FIELDS = (
    'batch_number', 'run_index', 'source_run_id', 'stored_source_score', 'source_score', 'candidate_run_id',
    'candidate_score', 'score_delta', 'accepted_as_new_best',
    'best_score_so_far', 'runtime_seconds', 'seed', 'stopped_reason',
    'optimizer_initial_score', 'initial_matches_source',
    'source_recomputed_reported_score', 'score_audit_total_score',
    'final_matches_score_audit',
    'assignments_same_at_start', 'source_assignments_copied',
    'assignments_changed_before_first_score',
    'assignments_removed_after_final_scoring',
    'visible_assignments_added_after_final_scoring',
    'status', 'run_kind', 'initial_score', 'final_total_score',
    'assignments_made', 'unfilled_shifts', 'timed_out', 'error',
)


def summarize_trials(trials):
    successful = [trial for trial in trials if trial['status'] == OptimizerRun.Status.COMPLETED]
    scores = [trial['final_total_score'] for trial in successful]
    runtimes = [trial['runtime_seconds'] for trial in successful]
    improved = sum(
        trial['final_total_score'] < trial['initial_score'] for trial in successful
    )
    return {
        'attempted_runs': len(trials),
        'completed_runs': len(successful),
        'failed_runs': len(trials) - len(successful),
        'best_final_score': min(scores) if scores else None,
        'median_final_score': median(scores) if scores else None,
        'worst_final_score': max(scores) if scores else None,
        'average_runtime_seconds': mean(runtimes) if runtimes else None,
        'best_seed': successful[scores.index(min(scores))]['seed'] if scores else None,
        'score_spread': max(scores) - min(scores) if scores else None,
        'any_run_improved_from_initial': bool(improved),
        'improved_runs': improved,
    }


def summarize_best_chain(initial_score, best_score, best_run_id, trials, total_runtime):
    accepted_scores = [
        trial['best_score_so_far']
        for trial in trials
        if trial['accepted_as_new_best']
    ]
    improvement = initial_score - best_score
    percent = (improvement / initial_score * 100) if initial_score else None
    monotonic = all(
        later < earlier
        for earlier, later in zip(
            [initial_score, *accepted_scores],
            accepted_scores,
        )
    )
    return {
        'initial_score': initial_score,
        'best_final_score': best_score,
        'absolute_improvement': improvement,
        'percent_improvement': percent,
        'accepted_improvements': len(accepted_scores),
        'best_run_id': best_run_id,
        'total_runtime_seconds': total_runtime,
        'accepted_bests_monotonically_improved': monotonic,
    }


class Command(BaseCommand):
    help = (
        'Benchmark optimizer runs against one BUILD or PREVIEW schedule. BENCHMARK '
        'runs are rollback-only by default. Use --retain-best with best-chain to '
        'persist only the final best benchmark run.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--schedule-block-id', type=int, required=True)
        parser.add_argument('--domain', required=True, help='Domain name (case-insensitive).')
        parser.add_argument('--runs', type=int, default=25)
        parser.add_argument(
            '--mode', choices=('independent', 'best-chain'), default='independent',
            help='Use best-chain for the primary Atlas schedule-improvement benchmark.',
        )
        parser.add_argument(
            '--start-mode', choices=('fresh_fill', 'current_schedule'),
            default='fresh_fill',
            help='Independent mode only.',
        )
        parser.add_argument('--time-limit-seconds', type=float)
        parser.add_argument(
            '--seed-base', type=int,
            help='Fix a deterministic seed sequence. Omit for stochastic benchmark search.',
        )
        parser.add_argument('--output-json')
        parser.add_argument('--output-csv')
        parser.add_argument(
            '--retain-best', action='store_true',
            help='Best-chain only: persist only the final best benchmark-created run.',
        )
        parser.add_argument(
            '--source-run-id', type=int,
            help='Best-chain only: start from this completed run in the selected Schedule Version.',
        )
        parser.add_argument(
            '--until-stalled', action='store_true',
            help='Best-chain only: continue running bounded batches until improvement stalls.',
        )
        parser.add_argument('--batch-runs', type=int, default=10)
        parser.add_argument('--max-batches', type=int, default=10)
        parser.add_argument('--stall-batches', type=int, default=3)

    def handle(self, *args, **options):
        if options['runs'] < 1:
            raise CommandError('--runs must be at least 1.')
        if options['time_limit_seconds'] is not None and options['time_limit_seconds'] <= 0:
            raise CommandError('--time-limit-seconds must be greater than 0.')
        if options['retain_best'] and options['mode'] != 'best-chain':
            raise CommandError('--retain-best requires --mode best-chain.')
        if options['source_run_id'] is not None and options['mode'] != 'best-chain':
            raise CommandError('--source-run-id requires --mode best-chain.')
        if options['until_stalled'] and options['mode'] != 'best-chain':
            raise CommandError('--until-stalled requires --mode best-chain.')
        for option_name in ('batch_runs', 'max_batches', 'stall_batches'):
            if options[option_name] < 1:
                raise CommandError(f'--{option_name.replace("_", "-")} must be at least 1.')

        versions = ScheduleVersion.objects.select_related('schedule_block', 'domain').filter(
            schedule_block_id=options['schedule_block_id'],
            domain__name__iexact=options['domain'],
            status=ScheduleVersion.Status.BUILD,
        )
        count = versions.count()
        if count != 1:
            raise CommandError(
                f'Expected exactly one BUILD Schedule Version for schedule block '
                f'{options["schedule_block_id"]} and domain {options["domain"]!r}; found {count}.'
            )
        version = versions.get()
        allowed_block_statuses = (
            version.schedule_block.BuildStatus.BUILD,
            version.schedule_block.BuildStatus.PREVIEW,
        )
        if version.schedule_block.build_status not in allowed_block_statuses:
            raise CommandError('Schedule Block must be in BUILD or PREVIEW status.')
        status_before = version.schedule_block.build_status

        persistence_mode = 'retain-best' if options['retain_best'] else 'rollback'
        randomness_mode = 'deterministic' if options['seed_base'] is not None else 'stochastic'
        self.stdout.write(f'persistence_mode: {persistence_mode}')
        self.stdout.write(f'randomness_mode: {randomness_mode}')
        self.stdout.write(
            'Safety: only the final best BENCHMARK run will be retained; source and losing runs '
            'will not be mutated.' if options['retain_best'] else
            'Safety: BENCHMARK runs are temporary and rolled back; no runs or schedule changes are retained.'
        )
        self.stdout.write(f'Benchmark mode: {options["mode"]}')
        self.stdout.write(f'Schedule block status before benchmark: {status_before}')

        limit_context = (
            patch.object(optimizer, 'MAX_RUNTIME_SECONDS', options['time_limit_seconds'])
            if options['time_limit_seconds'] is not None
            else nullcontext()
        )
        with limit_context:
            if options['until_stalled']:
                trials, summary, batches = self._run_until_stalled(version, options)
            elif options['mode'] == 'best-chain':
                trials, summary = self._run_best_chain(version, options)
                batches = []
            else:
                trials, summary = self._run_independent(version, options)
                batches = []

        version.schedule_block.refresh_from_db(fields=['build_status'])
        status_after = version.schedule_block.build_status
        report = {
            'benchmark_version': 4,
            'mode': options['mode'],
            'persistence_mode': persistence_mode,
            'schedule_block_id': version.schedule_block_id,
            'schedule_version_id': version.id,
            'domain': version.domain.name,
            'runs': options['runs'],
            'seed_base': options['seed_base'],
            'randomness_mode': randomness_mode,
            'time_limit_seconds': options['time_limit_seconds'],
            'until_stalled': options['until_stalled'],
            'batch_runs': options['batch_runs'] if options['until_stalled'] else None,
            'max_batches': options['max_batches'] if options['until_stalled'] else None,
            'stall_batches': options['stall_batches'] if options['until_stalled'] else None,
            'schedule_block_status_before': status_before,
            'schedule_block_status_after': status_after,
            'run_kind': 'BENCHMARK',
            'source_run_id': summary.get('source_run_id'),
            'retained_best_run_id': summary.get('retained_best_run_id'),
            'schedule_changes_retained': summary.get(
                'schedule_changes_retained', bool(summary.get('retained_best_run_id')),
            ),
            'safety': (
                'Only each improving batch best BENCHMARK run was retained; sources and losing runs were preserved/removed.'
                if summary.get('schedule_changes_retained') and options['until_stalled'] else
                'Only the final best BENCHMARK run was retained; source and losing runs were preserved/removed.'
                if summary.get('schedule_changes_retained') else
                'No benchmark-created run was retained because the persisted source remained best.'
                if options['retain_best'] else
                'All BENCHMARK runs rolled back; Build Workspace state unchanged.'
            ),
            'summary': summary,
            'batches': batches,
            'trials': trials,
        }
        if options['output_json']:
            self._write_json(Path(options['output_json']), report)
        if options['output_csv']:
            self._write_csv(Path(options['output_csv']), trials)

        self.stdout.write(f'Schedule block status after benchmark: {status_after}')
        self.stdout.write(f'source_run_id: {summary.get("source_run_id")}')
        self.stdout.write(f'retained_best_run_id: {summary.get("retained_best_run_id")}')
        self.stdout.write(
            f'schedule_changes_retained: {"yes" if report["schedule_changes_retained"] else "no"}'
        )
        self.stdout.write('\nBenchmark summary')
        for key, value in summary.items():
            self.stdout.write(f'{key}: {value}')

    def _run_until_stalled(self, version, options):
        started_at = monotonic()
        all_trials = []
        batches = []
        consecutive_stalls = 0
        current_source_run_id = options.get('source_run_id')
        initial_source_run_id = current_source_run_id
        initial_score = None
        final_score = None
        total_optimizer_runs = 0
        stopped_reason = 'max_batches'
        used_seeds = set()

        def run_batches():
            nonlocal consecutive_stalls, current_source_run_id
            nonlocal initial_source_run_id, initial_score, final_score
            nonlocal total_optimizer_runs, stopped_reason
            for batch_number in range(1, options['max_batches'] + 1):
                batch_seeds = self._attempt_seeds(
                    options,
                    options['batch_runs'],
                    deterministic_offset=total_optimizer_runs,
                    used_seeds=used_seeds,
                )
                batch_options = {
                    **options,
                    'runs': options['batch_runs'],
                    'source_run_id': current_source_run_id,
                    '_attempt_seeds': batch_seeds,
                    '_defer_finalization': not options['retain_best'],
                }
                batch_started_at = monotonic()
                batch_trials, batch_summary = self._run_best_chain(version, batch_options)
                batch_runtime = monotonic() - batch_started_at
                source_run_id = batch_summary['source_run_id']
                source_score = batch_summary['initial_score']
                best_score = batch_summary['best_final_score']
                improved = best_score < source_score
                durable_retained_id = batch_summary.get('retained_best_run_id')
                temporary_best_id = batch_summary.get('temporary_best_run_id')
                next_source_id = durable_retained_id or temporary_best_id or source_run_id
                if initial_score is None:
                    initial_score = source_score
                    initial_source_run_id = source_run_id
                final_score = best_score
                total_optimizer_runs += len(batch_trials)
                for trial in batch_trials:
                    all_trials.append({**trial, 'batch_number': batch_number})
                batch_row = {
                    'batch_number': batch_number,
                    'source_run_id': source_run_id,
                    'source_score': source_score,
                    'best_score_after_batch': best_score,
                    'retained_best_run_id': durable_retained_id,
                    'improvement': improved,
                    'absolute_improvement': source_score - best_score,
                    'runtime_seconds': batch_runtime,
                    'seeds': batch_seeds,
                }
                batches.append(batch_row)
                self.stdout.write(
                    f'batch={batch_number} source_run_id={source_run_id} '
                    f'source_score={source_score} best_score_after_batch={best_score} '
                    f'retained_best_run_id={durable_retained_id} '
                    f'improvement={"yes" if improved else "no"} '
                    f'absolute_improvement={source_score - best_score} '
                    f'seeds={batch_seeds} '
                    f'runtime_seconds={batch_runtime:.3f}'
                )
                if improved:
                    current_source_run_id = next_source_id
                    consecutive_stalls = 0
                else:
                    consecutive_stalls += 1
                if consecutive_stalls >= options['stall_batches']:
                    stopped_reason = 'stalled'
                    break

        if options['retain_best']:
            run_batches()
        else:
            with transaction.atomic():
                run_batches()
                transaction.set_rollback(True)

        improvement = initial_score - final_score
        percent = (improvement / initial_score * 100) if initial_score else None
        final_retained_id = current_source_run_id if options['retain_best'] else None
        any_retained = any(batch['retained_best_run_id'] is not None for batch in batches)
        summary = {
            'initial_source_run_id': initial_source_run_id,
            'source_run_id': initial_source_run_id,
            'initial_source_score': initial_score,
            'initial_score': initial_score,
            'final_best_score': final_score,
            'best_final_score': final_score,
            'retained_best_run_id': final_retained_id,
            'schedule_changes_retained': any_retained,
            'total_absolute_improvement': improvement,
            'absolute_improvement': improvement,
            'percent_improvement': percent,
            'total_batches': len(batches),
            'total_optimizer_runs': total_optimizer_runs,
            'total_runtime_seconds': monotonic() - started_at,
            'stopped_reason': stopped_reason,
        }
        return all_trials, summary, batches

    @staticmethod
    def _attempt_seeds(options, count, deterministic_offset=0, used_seeds=None):
        if options.get('seed_base') is not None:
            return [
                options['seed_base'] + deterministic_offset + index
                for index in range(count)
            ]
        used = used_seeds if used_seeds is not None else set()
        seeds = []
        while len(seeds) < count:
            seed = secrets.randbits(63)
            if seed in used:
                continue
            used.add(seed)
            seeds.append(seed)
        return seeds

    def _initial_seed(self, options):
        if options.get('seed_base') is not None:
            return options['seed_base'] - 1
        return self._attempt_seeds(options, 1)[0]

    def _optimize(self, version, seed, start_mode, source_run=None):
        return optimizer.optimize_schedule_version(
            version,
            seed=seed,
            start_mode=start_mode,
            source_run=source_run,
            run_kind='BENCHMARK',
            allow_preview_benchmark=True,
        )

    def _run_independent(self, version, options):
        start_mode = {
            'fresh_fill': OptimizerRun.StartMode.FRESH_FILL,
            'current_schedule': OptimizerRun.StartMode.CURRENT_SCHEDULE,
        }[options['start_mode']]
        source_run = None
        if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE:
            source_run = OptimizerRun.objects.filter(
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
                is_active=True,
            ).first()
            if source_run is None:
                raise CommandError('current_schedule requires an active completed optimizer run.')

        trials = []
        attempt_seeds = options.get('_attempt_seeds') or self._attempt_seeds(
            options, options['runs'],
        )
        for run_index, seed in enumerate(attempt_seeds, start=1):
            started_at = monotonic()
            try:
                with transaction.atomic():
                    payload = self._optimize(version, seed, start_mode, source_run)
                    created_run = OptimizerRun.objects.get(id=payload['optimizer_run_id'])
                    trial = self._independent_trial(run_index, seed, payload, created_run, started_at)
                    transaction.set_rollback(True)
            except Exception as exc:  # One bad seed must not abort an independent benchmark.
                trial = self._error_trial(run_index, seed, exc, started_at)
            trials.append(trial)
            self.stdout.write(
                f'run={run_index} kind={trial["run_kind"]} '
                f'candidate_run_id={trial["candidate_run_id"]} seed={seed} status={trial["status"]} '
                f'initial={trial["initial_score"]} final={trial["final_total_score"]} '
                f'seconds={trial["runtime_seconds"]:.3f}'
            )
        return trials, summarize_trials(trials)

    def _run_best_chain(self, version, options):
        trials = []
        chain_started_at = monotonic()
        with transaction.atomic():
            existing_run_ids = set(version.optimizer_runs.values_list('id', flat=True))
            original_active_run_id = version.optimizer_runs.filter(
                is_active=True,
            ).values_list('id', flat=True).first()
            original_locked_open_ids = list(
                ScheduleShiftInstance.objects.filter(
                    schedule_version=version, is_locked_open=True,
                ).values_list('id', flat=True)
            )
            original_version_summary = version.optimizer_summary
            original_version_score_is_stale = version.score_is_stale
            if options['source_run_id'] is not None:
                best_run = OptimizerRun.objects.filter(
                    id=options['source_run_id'], schedule_version=version,
                    status=OptimizerRun.Status.COMPLETED, final_score__isnull=False,
                ).first()
                if best_run is None:
                    raise CommandError(
                        '--source-run-id must identify a completed scored run in the selected Schedule Version.'
                    )
            else:
                best_run = OptimizerRun.objects.filter(
                    schedule_version=version,
                    status=OptimizerRun.Status.COMPLETED,
                    is_active=True,
                    final_score__isnull=False,
                ).first()
            if best_run is None:
                initial_payload = self._optimize(
                    version,
                    self._initial_seed(options),
                    OptimizerRun.StartMode.FRESH_FILL,
                )
                best_run = OptimizerRun.objects.get(id=initial_payload['optimizer_run_id'])
                if best_run.status != OptimizerRun.Status.COMPLETED:
                    raise CommandError('Unable to create a completed initial schedule for best-chain.')
                self.stdout.write(
                    f'Initial plausible schedule created: run_id={best_run.id} '
                    f'score={float(best_run.final_score)} seed={best_run.seed}'
                )
            else:
                self.stdout.write(
                    f'Initial plausible schedule selected: run_id={best_run.id} '
                    f'score={float(best_run.final_score)}'
                )

            initial_source_run_id = best_run.id
            initial_snapshot = self._source_snapshot(version, best_run)
            initial_score = initial_snapshot['score']
            best_score = initial_score
            attempt_seeds = options.get('_attempt_seeds') or self._attempt_seeds(
                options, options['runs'],
            )
            for run_index, seed in enumerate(attempt_seeds, start=1):
                source_run = best_run
                source_snapshot = self._source_snapshot(version, source_run)
                source_score = source_snapshot['score']
                stored_source_score = (
                    float(source_run.final_score)
                    if source_run.final_score is not None else None
                )
                started_at = monotonic()
                try:
                    with transaction.atomic():
                        payload = self._optimize(
                            version,
                            seed,
                            OptimizerRun.StartMode.CURRENT_SCHEDULE,
                            source_run,
                        )
                        candidate_run = OptimizerRun.objects.get(id=payload['optimizer_run_id'])
                    candidate_score = float(candidate_run.final_score)
                    optimizer_initial_score = float(payload['initial_score'])
                    debug = payload.get('debug') or {}
                    self._assert_source_start_matches(
                        source_run=source_run,
                        stored_source_score=stored_source_score,
                        source_snapshot=source_snapshot,
                        optimizer_initial_score=optimizer_initial_score,
                        debug=debug,
                    )
                    candidate_snapshot = self._source_snapshot(version, candidate_run)
                    if candidate_snapshot['score'] != candidate_score:
                        raise CommandError(
                            'Benchmark candidate persisted-state score mismatch: '
                            f'candidate_run_id={candidate_run.id} '
                            f'stored_candidate_score={candidate_score} '
                            f'candidate_recomputed_score={candidate_snapshot["score"]} '
                            f'candidate_assignment_count={candidate_snapshot["assignment_count"]}.'
                        )
                    score_audit_total = (debug.get('score_audit') or {}).get('total_score')
                    accepted = (
                        candidate_run.status == OptimizerRun.Status.COMPLETED
                        and candidate_score < best_score
                    )
                    if accepted:
                        best_run = candidate_run
                        best_score = candidate_score
                    trial = {
                        'run_index': run_index,
                        'source_run_id': source_run.id,
                        'source_score': source_score,
                        'stored_source_score': stored_source_score,
                        'candidate_run_id': candidate_run.id,
                        'candidate_score': candidate_score,
                        'score_delta': candidate_score - source_score,
                        'accepted_as_new_best': accepted,
                        'best_score_so_far': best_score,
                        'runtime_seconds': float(payload.get('runtime_seconds') or (monotonic() - started_at)),
                        'seed': seed,
                        'stopped_reason': payload.get('stopped_reason'),
                        'optimizer_initial_score': optimizer_initial_score,
                        'initial_matches_source': optimizer_initial_score == source_score,
                        'source_recomputed_reported_score': debug.get(
                            'source_state_reported_score_before_pre_score_changes'
                        ),
                        'score_audit_total_score': score_audit_total,
                        'final_matches_score_audit': (
                            score_audit_total == candidate_score
                            if score_audit_total is not None else None
                        ),
                        'assignments_same_at_start': debug.get('assignments_same_at_start'),
                        'source_assignments_copied': debug.get('copied_start_assignment_count'),
                        'assignments_changed_before_first_score': debug.get(
                            'assignments_changed_before_first_score'
                        ),
                        'assignments_removed_after_final_scoring': debug.get(
                            'assignments_removed_after_final_scoring'
                        ),
                        'visible_assignments_added_after_final_scoring': debug.get(
                            'visible_assignments_added_after_final_scoring'
                        ),
                        'status': candidate_run.status,
                        'run_kind': candidate_run.run_kind,
                        'initial_score': float(payload['initial_score']),
                        'final_total_score': candidate_score,
                        'assignments_made': payload.get('assignments_made'),
                        'unfilled_shifts': payload.get('unfilled_shift_count'),
                        'timed_out': bool(payload.get('timed_out')),
                        'error': None,
                    }
                except CommandError:
                    raise
                except Exception as exc:
                    trial = self._error_trial(
                        run_index, seed, exc, started_at,
                        source_run_id=source_run.id,
                        source_score=source_score,
                        best_score=best_score,
                    )
                trials.append(trial)
                self.stdout.write(
                    f'iteration={run_index} source_run_id={trial["source_run_id"]} '
                    f'stored_source_score={trial["stored_source_score"]} '
                    f'source_recomputed_score={trial["source_score"]} '
                    f'optimizer_initial_score={trial["optimizer_initial_score"]} '
                    f'candidate_run_id={trial["candidate_run_id"]} '
                    f'candidate_score={trial["candidate_score"]} '
                    f'initial_matches_source={"yes" if trial["initial_matches_source"] else "no"} '
                    f'source_recomputed_reported_score={trial["source_recomputed_reported_score"]} '
                    f'score_audit_total_score={trial["score_audit_total_score"]} '
                    f'final_matches_score_audit={self._yes_no_unknown(trial["final_matches_score_audit"])} '
                    f'assignments_same_at_start={self._yes_no_unknown(trial["assignments_same_at_start"])} '
                    f'source_assignments_copied={trial["source_assignments_copied"]} '
                    f'assignments_changed_before_first_score={trial["assignments_changed_before_first_score"]} '
                    f'assignments_removed_after_final_scoring={trial["assignments_removed_after_final_scoring"]} '
                    f'visible_assignments_added_after_final_scoring={trial["visible_assignments_added_after_final_scoring"]} '
                    f'score_delta={trial["score_delta"]} '
                    f'accepted_as_new_best={"yes" if trial["accepted_as_new_best"] else "no"} '
                    f'best_score_so_far={trial["best_score_so_far"]} '
                    f'runtime_seconds={trial["runtime_seconds"]:.3f} seed={seed} '
                    f'stopped_reason={trial["stopped_reason"]} error={trial["error"]}'
                )

            summary = summarize_best_chain(
                initial_score,
                best_score,
                best_run.id,
                trials,
                monotonic() - chain_started_at,
            )
            created_run_ids = set(version.optimizer_runs.values_list('id', flat=True)) - existing_run_ids
            retained_best_run_id = None
            defer_finalization = options.get('_defer_finalization', False)
            if defer_finalization:
                summary['temporary_best_run_id'] = best_run.id
            elif options['retain_best'] and best_run.id in created_run_ids:
                retained_best_run_id = best_run.id
                losing_ids = created_run_ids - {best_run.id}
                self._delete_benchmark_runs(version, losing_ids)
                self._mark_retained_best(best_run)
            elif options['retain_best']:
                self._delete_benchmark_runs(version, created_run_ids)
            if options['retain_best']:
                self._restore_workspace_state(
                    version, original_active_run_id, original_locked_open_ids,
                    original_version_summary, original_version_score_is_stale,
                )
            elif not defer_finalization:
                transaction.set_rollback(True)
            summary['source_run_id'] = initial_source_run_id
            summary['retained_best_run_id'] = retained_best_run_id
            if defer_finalization:
                summary.pop('best_run_id', None)
            elif options['retain_best']:
                summary.pop('best_run_id', None)
            else:
                summary['temporary_best_run_id'] = summary.pop('best_run_id')
        return trials, summary

    def _delete_benchmark_runs(self, version, run_ids):
        if not run_ids:
            return
        ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            optimizer_run_id__in=run_ids,
        ).delete()
        OptimizerRun.objects.filter(
            schedule_version=version, id__in=run_ids, run_kind='BENCHMARK',
        ).delete()

    def _mark_retained_best(self, best_run):
        best_run.notes = 'Retained optimizer benchmark best-chain result.'
        best_run.optimizer_summary = {
            **(best_run.optimizer_summary or {}),
            'benchmark_result': 'best-chain',
            'retained_best': True,
        }
        best_run.save(update_fields=['notes', 'optimizer_summary'])

    def _restore_workspace_state(
        self, version, active_run_id, locked_open_ids, optimizer_summary, score_is_stale,
    ):
        OptimizerRun.objects.filter(schedule_version=version, is_active=True).update(is_active=False)
        if active_run_id is not None:
            OptimizerRun.objects.filter(
                schedule_version=version, id=active_run_id,
            ).update(is_active=True)
        ScheduleShiftInstance.objects.filter(schedule_version=version).update(is_locked_open=False)
        ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            id__in=locked_open_ids,
        ).update(is_locked_open=True)
        version.optimizer_summary = optimizer_summary
        version.score_is_stale = score_is_stale
        version.save(update_fields=['optimizer_summary', 'score_is_stale', 'updated_at'])

    def _source_snapshot(self, version, source_run):
        report = optimizer.build_violation_report(version, optimizer_run=source_run)
        rows = list(
            assignments_for_viewed_run(version, source_run)
            .order_by('id')
            .values('id', 'shift_instance_id', 'physician_id')
        )
        return {
            'score': float(report['total_score']),
            'assignment_count': len({
                (row['shift_instance_id'], row['physician_id']) for row in rows
            }),
            'assignment_ids': [row['id'] for row in rows],
            'shift_instance_ids': sorted({row['shift_instance_id'] for row in rows}),
        }

    def _assert_source_start_matches(
        self, source_run, stored_source_score, source_snapshot,
        optimizer_initial_score, debug,
    ):
        loaded_count = debug.get('copied_start_assignment_count')
        source_count = source_snapshot['assignment_count']
        scores_match = optimizer_initial_score == source_snapshot['score']
        assignments_match = (
            debug.get('assignments_same_at_start') is True
            and loaded_count == source_count
        )
        if scores_match and assignments_match:
            return
        raise CommandError(
            'Benchmark source/start correctness guard failed; aborting chain. '
            f'source_run_id={source_run.id} '
            f'stored_source_score={stored_source_score} '
            f'source_recomputed_score={source_snapshot["score"]} '
            f'optimizer_initial_score={optimizer_initial_score} '
            f'source_assignment_count={source_count} '
            f'optimizer_loaded_assignment_count={loaded_count} '
            f'source_assignment_ids={source_snapshot["assignment_ids"]} '
            f'source_shift_instance_ids={source_snapshot["shift_instance_ids"]} '
            f'missing_pairs={debug.get("source_pairs_missing_at_start")} '
            f'extra_pairs={debug.get("source_pairs_extra_at_start")}.'
        )

    def _independent_trial(self, run_index, seed, payload, created_run, started_at):
        final_score = float(payload['final_score'])
        initial_score = float(payload['initial_score'])
        return {
            'run_index': run_index,
            'source_run_id': None,
            'stored_source_score': None,
            'source_score': initial_score,
            'candidate_run_id': created_run.id,
            'candidate_score': final_score,
            'score_delta': final_score - initial_score,
            'accepted_as_new_best': None,
            'best_score_so_far': final_score,
            'runtime_seconds': float(payload.get('runtime_seconds') or (monotonic() - started_at)),
            'seed': seed,
            'stopped_reason': payload.get('stopped_reason'),
            'optimizer_initial_score': initial_score,
            'initial_matches_source': None,
            'source_recomputed_reported_score': None,
            'score_audit_total_score': (payload.get('debug') or {}).get('score_audit', {}).get('total_score'),
            'final_matches_score_audit': None,
            'assignments_same_at_start': (payload.get('debug') or {}).get('assignments_same_at_start'),
            'source_assignments_copied': (payload.get('debug') or {}).get('copied_start_assignment_count'),
            'assignments_changed_before_first_score': (payload.get('debug') or {}).get(
                'assignments_changed_before_first_score'
            ),
            'assignments_removed_after_final_scoring': (payload.get('debug') or {}).get(
                'assignments_removed_after_final_scoring'
            ),
            'visible_assignments_added_after_final_scoring': (payload.get('debug') or {}).get(
                'visible_assignments_added_after_final_scoring'
            ),
            'status': created_run.status,
            'run_kind': created_run.run_kind,
            'initial_score': initial_score,
            'final_total_score': final_score,
            'assignments_made': payload.get('assignments_made'),
            'unfilled_shifts': payload.get('unfilled_shift_count'),
            'timed_out': bool(payload.get('timed_out')),
            'error': None,
        }

    def _error_trial(
        self, run_index, seed, exc, started_at,
        source_run_id=None, source_score=None, best_score=None,
    ):
        return {
            'run_index': run_index,
            'source_run_id': source_run_id,
            'stored_source_score': None,
            'source_score': source_score,
            'candidate_run_id': None,
            'candidate_score': None,
            'score_delta': None,
            'accepted_as_new_best': False,
            'best_score_so_far': best_score,
            'runtime_seconds': monotonic() - started_at,
            'seed': seed,
            'stopped_reason': 'error',
            'optimizer_initial_score': None,
            'initial_matches_source': False,
            'source_recomputed_reported_score': None,
            'score_audit_total_score': None,
            'final_matches_score_audit': None,
            'assignments_same_at_start': None,
            'source_assignments_copied': None,
            'assignments_changed_before_first_score': None,
            'assignments_removed_after_final_scoring': None,
            'visible_assignments_added_after_final_scoring': None,
            'status': 'ERROR',
            'run_kind': 'BENCHMARK',
            'initial_score': None,
            'final_total_score': None,
            'assignments_made': None,
            'unfilled_shifts': None,
            'timed_out': False,
            'error': f'{type(exc).__name__}: {exc}',
        }

    @staticmethod
    def _yes_no_unknown(value):
        if value is None:
            return 'unknown'
        return 'yes' if value else 'no'

    def _write_json(self, path, report):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Wrote JSON: {path}'))

    def _write_csv(self, path, trials):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=TRIAL_FIELDS)
            writer.writeheader()
            writer.writerows(trials)
        self.stdout.write(self.style.SUCCESS(f'Wrote CSV: {path}'))
