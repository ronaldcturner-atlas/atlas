import time
from time import monotonic

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.scheduling.models import OptimizerControl, OptimizerRun
from apps.scheduling.optimizer import optimize_schedule_version


class Command(BaseCommand):
    help = 'Run queued optimizer jobs independently from web requests.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--poll-seconds', type=float, default=1.0)

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

    def _run_control(self, control_id):
        control = OptimizerControl.objects.select_related(
            'schedule_version__schedule_block', 'schedule_version__domain',
            'optimizer_run__created_by', 'source_run',
        ).get(pk=control_id)
        last_poll = [0.0]
        stopped = [False]

        def stop_requested():
            now = monotonic()
            if now - last_poll[0] >= 0.5:
                stopped[0] = OptimizerControl.objects.filter(
                    pk=control_id, stop_requested=True,
                ).exists()
                last_poll[0] = now
            return stopped[0]

        try:
            optimize_schedule_version(
                control.schedule_version,
                created_by=control.optimizer_run.created_by,
                optimizer_run=control.optimizer_run,
                seed=control.optimizer_run.seed,
                start_mode=control.optimizer_run.start_mode,
                source_run=control.source_run,
                adaptive_runtime=True,
                stop_requested=stop_requested,
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
            OptimizerControl.objects.filter(pk=control_id).delete()

    def handle(self, *args, **options):
        while True:
            control_id = self._claim_next()
            if control_id is not None:
                self.stdout.write(f'Running optimizer job {control_id}')
                self._run_control(control_id)
                if options['once']:
                    return
                continue
            if options['once']:
                return
            time.sleep(max(options['poll_seconds'], 0.1))
