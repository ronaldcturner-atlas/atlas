import time
import signal
import os
import socket
import traceback
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.scheduling.models import (
    OptimizerControl,
    OptimizerRun,
)
from apps.scheduling.optimizer import optimize_schedule_version
from apps.scheduling.optimizer_v2_runner import (
    V2_RUN_KINDS,
    _failure_configuration_snapshot,
    optimize_schedule_version_v2,
)
from apps.scheduling.trade_lifecycle import expire_started_trade_activity


DEFAULT_JOB_TIMEOUT_SECONDS = 16 * 60
_HELD_CONTROL_LOCKS = set()


def _code_release_identifier():
    return next((
        value for value in (
            os.environ.get('ATLAS_RELEASE'),
            os.environ.get('RAILWAY_GIT_COMMIT_SHA'),
            os.environ.get('SOURCE_VERSION'),
            os.environ.get('GIT_COMMIT'),
        ) if value
    ), 'development')


class OptimizerJobTimeout(TimeoutError):
    pass


class Command(BaseCommand):
    help = 'Run queued optimizer jobs independently from web requests.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--poll-seconds', type=float, default=1.0)
        parser.add_argument(
            '--job-timeout-seconds', type=float,
            default=None,
        )

    @staticmethod
    def _advisory_lock_key(control_id):
        # PostgreSQL advisory locks are released automatically when a worker
        # process or database connection disappears. This makes a started job
        # reclaimable after a local service restart without tying it to a web
        # session or browser connection.
        return control_id.int & ((1 << 63) - 1)

    def _try_control_lock(self, control_id):
        if control_id in _HELD_CONTROL_LOCKS:
            return False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_try_advisory_lock(%s)',
                    [self._advisory_lock_key(control_id)],
                )
                acquired = bool(cursor.fetchone()[0])
        else:
            acquired = True
        if acquired:
            _HELD_CONTROL_LOCKS.add(control_id)
        return acquired

    def _release_control_lock(self, control_id):
        if control_id not in _HELD_CONTROL_LOCKS:
            return
        try:
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_advisory_unlock(%s)',
                        [self._advisory_lock_key(control_id)],
                    )
        finally:
            _HELD_CONTROL_LOCKS.discard(control_id)

    def _claim_next(self):
        with transaction.atomic():
            controls = list(
                OptimizerControl.objects.select_for_update(skip_locked=True)
                .filter(optimizer_run__status=OptimizerRun.Status.RUNNING)
                .order_by('started_at', 'created_at')
            )
            for control in controls:
                if not self._try_control_lock(control.pk):
                    continue
                if control.started_at is None:
                    control.started_at = timezone.now()
                    control.save(update_fields=['started_at'])
                return control.pk
            return None

    @staticmethod
    def _store_live_best_score(control_id, score):
        """Commit progress outside the optimizer's long schedule transaction."""
        close_old_connections()
        try:
            OptimizerControl.objects.filter(pk=control_id).update(
                live_best_score=score,
                progress_updated_at=timezone.now(),
            )
        finally:
            close_old_connections()

    def _run_control(self, control_id, *, job_timeout_seconds=None):
        control = OptimizerControl.objects.select_related(
            'schedule_version__schedule_block', 'schedule_version__domain',
            'optimizer_run__created_by', 'source_run',
        ).get(pk=control_id)
        effective_job_timeout_seconds = (
            float(job_timeout_seconds)
            if job_timeout_seconds is not None
            else float(control.optimizer_run.max_runtime_seconds) + 60
        )
        last_poll = [0.0]
        stopped = [False]
        progress_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix='optimizer-progress',
        )
        latest_live_score = [None]
        submitted_live_score = [None]
        last_progress_publish = [0.0]

        def publish_progress(score, *, force=False):
            if score is None:
                return
            latest_live_score[0] = score
            now = monotonic()
            if not force and now - last_progress_publish[0] < 0.75:
                return
            if not force and score == submitted_live_score[0]:
                return
            last_progress_publish[0] = now
            submitted_live_score[0] = score
            progress_executor.submit(
                self._store_live_best_score,
                control_id,
                score,
            )

        def stop_requested():
            now = monotonic()
            if now - last_poll[0] >= 0.5:
                stopped[0] = OptimizerControl.objects.filter(
                    pk=control_id, stop_requested=True,
                ).exists()
                last_poll[0] = now
            return stopped[0]

        alarm_supported = hasattr(signal, 'SIGALRM') and hasattr(signal, 'setitimer')
        previous_handler = None
        if alarm_supported and effective_job_timeout_seconds > 0:
            previous_handler = signal.getsignal(signal.SIGALRM)

            def timeout_handler(_signum, _frame):
                raise OptimizerJobTimeout(
                    f'Optimizer exceeded the worker safety limit of '
                    f'{effective_job_timeout_seconds:g} seconds.'
                )

            signal.signal(signal.SIGALRM, timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, effective_job_timeout_seconds)
        try:
            if control.optimizer_run.run_kind in V2_RUN_KINDS:
                optimize_schedule_version_v2(
                    control.schedule_version,
                    created_by=control.optimizer_run.created_by,
                    optimizer_run=control.optimizer_run,
                    source_run=control.source_run,
                    stop_requested=stop_requested,
                    progress_callback=publish_progress,
                )
            else:
                optimize_schedule_version(
                    control.schedule_version,
                    created_by=control.optimizer_run.created_by,
                    optimizer_run=control.optimizer_run,
                    seed=control.optimizer_run.seed,
                    start_mode=control.optimizer_run.start_mode,
                    source_run=control.source_run,
                    max_runtime_seconds=control.optimizer_run.max_runtime_seconds,
                    optimization_focus=control.optimizer_run.optimization_focus,
                    adaptive_runtime=True,
                    stop_requested=stop_requested,
                    progress_callback=publish_progress,
                    isolated_run=bool(
                        getattr(settings, 'OPTIMIZER_ENABLE_PARALLEL_ISOLATION', False)
                    ),
                )
        except Exception as exc:
            failure_traceback = traceback.format_exc()
            self.stderr.write(failure_traceback)
            failed_run = OptimizerRun.objects.get(id=control.optimizer_run_id)
            debug = dict(failed_run.optimizer_debug or {})
            failure_diagnostic = dict(debug.get('failure_diagnostic') or {})
            failure_diagnostic.update({
                'terminal_status': OptimizerRun.Status.FAILED,
                'failed_at': timezone.now().isoformat(),
                'runtime_seconds': (
                    (timezone.now() - control.started_at).total_seconds()
                    if control.started_at is not None else None
                ),
                'exception_type': type(exc).__name__,
                'exception_message': str(exc),
                'traceback': failure_traceback,
                'optimizer_control_id': str(control.pk),
                'worker_hostname': socket.gethostname(),
                'worker_process_id': os.getpid(),
                'code_release': _code_release_identifier(),
                'last_reported_best_score': (
                    float(latest_live_score[0])
                    if latest_live_score[0] is not None else None
                ),
            })
            if 'configuration_snapshot' not in failure_diagnostic:
                try:
                    failure_diagnostic['configuration_snapshot'] = (
                        _failure_configuration_snapshot(
                            control.schedule_version
                        )
                    )
                except Exception as snapshot_exc:
                    failure_diagnostic['configuration_snapshot_error'] = {
                        'exception_type': type(snapshot_exc).__name__,
                        'exception_message': str(snapshot_exc),
                    }
            debug.update({
                'optimizer_engine': (
                    'V2' if failed_run.run_kind in V2_RUN_KINDS else 'V1'
                ),
                'failure_diagnostic': failure_diagnostic,
            })
            failed_run.status = OptimizerRun.Status.FAILED
            failed_run.is_active = False
            failed_run.optimizer_debug = debug
            failed_run.notes = (
                f'Background optimizer failed: {type(exc).__name__}: {exc}'
            )
            failed_run.save(update_fields=[
                'status', 'is_active', 'optimizer_debug', 'notes',
            ])
            self.stderr.write(self.style.ERROR(
                f'Optimizer Run {control.optimizer_run_id} failed: {exc}'
            ))
        finally:
            if latest_live_score[0] != submitted_live_score[0]:
                publish_progress(latest_live_score[0], force=True)
            progress_executor.shutdown(wait=True)
            if alarm_supported and effective_job_timeout_seconds > 0:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
            OptimizerControl.objects.filter(pk=control_id).delete()

    def handle(self, *args, **options):
        next_trade_cleanup = 0.0
        while True:
            now = monotonic()
            if now >= next_trade_cleanup:
                expire_started_trade_activity()
                next_trade_cleanup = now + 30.0
            control_id = self._claim_next()
            if control_id is not None:
                self.stdout.write(f'Running or resuming optimizer job {control_id}')
                try:
                    self._run_control(
                        control_id,
                        job_timeout_seconds=options['job_timeout_seconds'],
                    )
                finally:
                    self._release_control_lock(control_id)
                if options['once']:
                    return
                continue
            if options['once']:
                return
            time.sleep(max(options['poll_seconds'], 0.1))
