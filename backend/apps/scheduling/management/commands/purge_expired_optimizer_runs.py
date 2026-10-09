from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.scheduling.models import (
    OptimizerControl,
    OptimizerRun,
    ScheduleBlock,
    ScheduleShiftAssignment,
)


class Command(BaseCommand):
    help = (
        'Delete unused optimizer runs 30 days after the most recent publication, '
        'while preserving the current live schedule run.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        days = options['days']
        if days < 1:
            raise ValueError('--days must be at least 1.')

        cutoff = timezone.now() - timedelta(days=days)
        version_ids = list(
            ScheduleBlock.objects.filter(
                build_status=ScheduleBlock.BuildStatus.ARCHIVE,
                published_at__isnull=False,
                published_at__lte=cutoff,
            ).values_list('schedule_versions__id', flat=True)
        )
        published_run_ids = set(
            OptimizerRun.objects.filter(
                published_schedule_versions__id__in=version_ids,
            ).values_list('id', flat=True)
        )
        controlled_run_ids = set(
            OptimizerControl.objects.filter(
                schedule_version_id__in=version_ids,
                optimizer_run_id__isnull=False,
            ).values_list('optimizer_run_id', flat=True)
        )
        run_ids = list(
            OptimizerRun.objects.filter(schedule_version_id__in=version_ids)
            .exclude(id__in=published_run_ids | controlled_run_ids)
            .exclude(status=OptimizerRun.Status.RUNNING)
            .values_list('id', flat=True)
        )

        if options['dry_run']:
            self.stdout.write(
                f'Would delete {len(run_ids)} expired optimizer run(s).'
            )
            return

        with transaction.atomic():
            ScheduleShiftAssignment.objects.filter(optimizer_run_id__in=run_ids).delete()
            OptimizerRun.objects.filter(id__in=run_ids).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {len(run_ids)} expired optimizer run(s).'
        ))
