import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .backups import (
    create_postgres_backup,
    restore_postgres_backup,
    verify_postgres_backup,
)


DATABASE = {
    'ENGINE': 'django.db.backends.postgresql',
    'NAME': 'atlas',
    'USER': 'atlas_user',
    'PASSWORD': 'private-password',
    'HOST': 'database.internal',
    'PORT': '5432',
    'OPTIONS': {'sslmode': 'require'},
}


class DatabaseBackupTests(SimpleTestCase):
    def test_backup_is_atomic_checksummed_and_keeps_password_out_of_arguments(self):
        observed = {}

        def fake_run(command, **kwargs):
            observed['command'] = command
            observed['environment'] = kwargs['env']
            output_path = Path(command[command.index('--file') + 1])
            output_path.write_bytes(b'postgres-custom-archive')
            return subprocess.CompletedProcess(command, 0, '', '')

        with TemporaryDirectory() as directory, patch(
            'apps.common.backups.subprocess.run', side_effect=fake_run,
        ):
            output = Path(directory) / 'atlas.dump'
            backup_path, checksum_path, checksum = create_postgres_backup(DATABASE, output)

            self.assertEqual(backup_path, output.resolve())
            self.assertTrue(checksum_path.is_file())
            self.assertIn(checksum, checksum_path.read_text(encoding='utf-8'))
            self.assertNotIn(DATABASE['PASSWORD'], ' '.join(observed['command']))
            self.assertEqual(observed['environment']['PGPASSWORD'], DATABASE['PASSWORD'])
            self.assertEqual(observed['environment']['PGSSLMODE'], 'require')

    def test_verify_rejects_changed_backup_before_pg_restore(self):
        with TemporaryDirectory() as directory:
            backup = Path(directory) / 'atlas.dump'
            backup.write_bytes(b'original')
            checksum = backup.with_suffix('.dump.sha256')
            checksum.write_text('0' * 64 + '  atlas.dump\n', encoding='utf-8')

            with patch('apps.common.backups.subprocess.run') as run:
                with self.assertRaisesMessage(CommandError, 'checksum verification failed'):
                    verify_postgres_backup(backup)
                run.assert_not_called()

    def test_verify_checks_postgresql_archive_structure(self):
        with TemporaryDirectory() as directory:
            backup = Path(directory) / 'atlas.dump'
            backup.write_bytes(b'archive')
            import hashlib
            digest = hashlib.sha256(b'archive').hexdigest()
            backup.with_suffix('.dump.sha256').write_text(
                f'{digest}  atlas.dump\n', encoding='utf-8',
            )
            completed = subprocess.CompletedProcess(
                ['pg_restore'], 0, '; header\n1; object one\n2; object two\n', '',
            )
            with patch('apps.common.backups.subprocess.run', return_value=completed) as run:
                count = verify_postgres_backup(backup)

            self.assertEqual(count, 2)
            self.assertEqual(run.call_args.args[0][:2], ['pg_restore', '--list'])

    def test_restore_verifies_archive_and_keeps_password_out_of_arguments(self):
        with TemporaryDirectory() as directory, patch(
            'apps.common.backups.verify_postgres_backup', return_value=2,
        ) as verify, patch('apps.common.backups.subprocess.run') as run:
            backup = Path(directory) / 'atlas.dump'
            backup.write_bytes(b'archive')

            restore_postgres_backup(DATABASE, backup)

            verify.assert_called_once_with(backup)
            command = run.call_args.args[0]
            self.assertIn('--clean', command)
            self.assertIn('--if-exists', command)
            self.assertIn('--exit-on-error', command)
            self.assertIn('--single-transaction', command)
            self.assertNotIn(DATABASE['PASSWORD'], ' '.join(command))
            self.assertEqual(run.call_args.kwargs['env']['PGPASSWORD'], DATABASE['PASSWORD'])
