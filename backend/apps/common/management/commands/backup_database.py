import os
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.common.backups import create_postgres_backup


class Command(BaseCommand):
    help = 'Create an atomic PostgreSQL custom-format backup and SHA-256 checksum.'

    def add_arguments(self, parser):
        parser.add_argument('--output')

    def handle(self, *args, **options):
        output = options.get('output')
        if not output:
            backup_directory = os.environ.get('ATLAS_BACKUP_DIR', '').strip()
            if not backup_directory:
                raise CommandError('Provide --output or configure ATLAS_BACKUP_DIR.')
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            output = Path(backup_directory) / f'atlas-{timestamp}.dump'
        output_path, checksum_path, checksum = create_postgres_backup(
            settings.DATABASES['default'],
            output,
        )
        self.stdout.write(self.style.SUCCESS(f'Backup created: {output_path}'))
        self.stdout.write(f'Checksum file: {checksum_path}')
        self.stdout.write(f'SHA-256: {checksum}')
