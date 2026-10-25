from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.scheduling.models import ScheduleRequestBulkOperation


class Command(BaseCommand):
    help = 'Delete temporary request-group audit records after their request dates pass.'

    def handle(self, *args, **options):
        deleted, _ = ScheduleRequestBulkOperation.objects.filter(
            expires_after__lt=timezone.localdate(),
        ).delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted {deleted} expired request-group audit records.'))
