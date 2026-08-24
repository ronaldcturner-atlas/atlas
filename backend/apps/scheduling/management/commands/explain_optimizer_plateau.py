import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.scheduling.models import OptimizerRun, ScheduleVersion
from apps.scheduling.plateau_diagnostics import build_plateau_explanation


class Command(BaseCommand):
    help = 'Explain remaining violations and tested local moves for one completed optimizer run.'

    def add_arguments(self, parser):
        parser.add_argument('--schedule-block-id', type=int, required=True)
        parser.add_argument('--domain', required=True)
        parser.add_argument('--optimizer-run-id', type=int, required=True)
        parser.add_argument('--output-json')

    def handle(self, *args, **options):
        versions = ScheduleVersion.objects.select_related('schedule_block', 'domain').filter(
            schedule_block_id=options['schedule_block_id'],
            domain__name__iexact=options['domain'],
            status=ScheduleVersion.Status.BUILD,
        )
        if versions.count() != 1:
            raise CommandError('Expected exactly one BUILD Schedule Version for the block and domain.')
        version = versions.get()
        optimizer_run = OptimizerRun.objects.filter(
            id=options['optimizer_run_id'],
            schedule_version=version,
            status=OptimizerRun.Status.COMPLETED,
        ).first()
        if optimizer_run is None:
            raise CommandError(
                '--optimizer-run-id must identify a completed run in the selected Schedule Version.'
            )

        report = build_plateau_explanation(version, optimizer_run)
        rendered = json.dumps(report, indent=2, sort_keys=True)
        if options['output_json']:
            path = Path(options['output_json'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered + '\n', encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f'Wrote JSON: {path}'))
            self.stdout.write(json.dumps(report['summary'], indent=2, sort_keys=True))
        else:
            self.stdout.write(rendered)
