import time
import signal
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.scheduling.models import OptimizerControl, OptimizerRun
from apps.scheduling.optimizer import optimize_schedule_version


DEFAULT_JOB_TIMEOUT_SECONDS = 16 * 60


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

    def _claim_next(self):
        with transaction.atomic():
            control = (
                OptimizerControl.objects.select_for_update(skip_locked=True)
                .filter(started_at__isnull=True, optimizer_run__status=OptimizerRun.Status.RUNNING)
                .order_by('created_at')
                .first()
            )
            if control is None:
                return None
            control.started_at = timezone.now()
            control.save(update_fields=['started_at'])
            return control.pk

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
            optimize_schedule_version(
                control.schedule_version,
                created_by=control.optimizer_run.created_by,
                optimizer_run=control.optimizer_run,
                seed=control.optimizer_run.seed,
                start_mode=control.optimizer_run.start_mode,
                source_run=control.source_run,
                max_runtime_seconds=control.optimizer_run.max_runtime_seconds,
                adaptive_runtime=True,
                stop_requested=stop_requested,
                progress_callback=publish_progress,
            )
        except Exception as exc:
            OptimizerRun.objects.filter(
                id=control.optimizer_run_id,
                status=OptimizerRun.Status.RUNNING,
            ).update(
                status=OptimizerRun.Status.FAILED,
                is_active=False,
                notes=f'Background optimizer failed: {type(exc).__name__}: {exc}',
            )
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
        while True:
            control_id = self._claim_next()
            if control_id is not None:
                self.stdout.write(f'Running optimizer job {control_id}')
                self._run_control(
                    control_id,
                    job_timeout_seconds=options['job_timeout_seconds'],
                )
                if options['once']:
                    return
                continue
            if options['once']:
                return
            time.sleep(max(options['poll_seconds'], 0.1))
