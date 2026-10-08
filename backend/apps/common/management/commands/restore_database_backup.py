from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.common.backups import restore_postgres_backup


class Command(BaseCommand):
    help = 'Restore a verified Atlas PostgreSQL backup after explicit database-name confirmation.'

    def add_arguments(self, parser):
        parser.add_argument('backup_path')
        parser.add_argument('--confirm-database', required=True)

    def handle(self, *args, **options):
        database_name = str(settings.DATABASES['default'].get('NAME', ''))
        if options['confirm_database'] != database_name:
            raise CommandError(
                'Restore confirmation does not exactly match the configured database name.'
            )
        restore_postgres_backup(settings.DATABASES['default'], options['backup_path'])
        self.stdout.write(self.style.SUCCESS('Database restore completed.'))
