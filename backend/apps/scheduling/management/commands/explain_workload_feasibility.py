import json

from django.core.management.base import BaseCommand, CommandError

from apps.scheduling.models import OptimizerRun, ScheduleVersion
from apps.scheduling.workload_feasibility import build_workload_feasibility


class Command(BaseCommand):
    help = 'Explain aggregate workload-hour feasibility without running optimizer search.'

    def add_arguments(self, parser):
        parser.add_argument('--schedule-block-id', type=int, required=True)
        parser.add_argument('--domain', required=True)
        parser.add_argument('--optimizer-run-id', type=int)

    def handle(self, *args, **options):
        versions = ScheduleVersion.objects.select_related('schedule_block', 'domain').filter(
            schedule_block_id=options['schedule_block_id'],
            domain__name__iexact=options['domain'],
            status=ScheduleVersion.Status.BUILD,
        )
        if versions.count() != 1:
            raise CommandError('Expected exactly one BUILD Schedule Version for the block and domain.')
        version = versions.get()

        optimizer_run = None
        if options['optimizer_run_id'] is not None:
            optimizer_run = OptimizerRun.objects.filter(
                id=options['optimizer_run_id'],
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
            ).first()
            if optimizer_run is None:
                raise CommandError(
                    '--optimizer-run-id must identify a completed run in the selected Schedule Version.'
                )

        report = build_workload_feasibility(version, optimizer_run)
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
