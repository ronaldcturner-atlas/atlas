from django.core.management.base import BaseCommand

from apps.common.backups import verify_postgres_backup


class Command(BaseCommand):
    help = 'Verify an Atlas backup checksum and PostgreSQL archive structure.'

    def add_arguments(self, parser):
        parser.add_argument('backup_path')

    def handle(self, *args, **options):
        object_count = verify_postgres_backup(options['backup_path'])
        self.stdout.write(self.style.SUCCESS(
            f'Backup verified successfully ({object_count} archive objects).'
        ))
