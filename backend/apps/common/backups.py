import hashlib
import hmac
import os
import subprocess
import uuid
from pathlib import Path

from django.core.management.base import CommandError


def _postgres_configuration(database):
    if database.get('ENGINE') != 'django.db.backends.postgresql':
        raise CommandError('Atlas database backups require PostgreSQL.')
    required = ('NAME', 'USER', 'PASSWORD', 'HOST', 'PORT')
    missing = [name for name in required if not str(database.get(name, '')).strip()]
    if missing:
        raise CommandError('The PostgreSQL connection configuration is incomplete.')
    return database


def _postgres_environment(database):
    environment = os.environ.copy()
    environment['PGPASSWORD'] = str(database['PASSWORD'])
    ssl_mode = (database.get('OPTIONS') or {}).get('sslmode')
    if ssl_mode:
        environment['PGSSLMODE'] = str(ssl_mode)
    return environment


def _connection_arguments(database):
    return [
        '--host', str(database['HOST']),
        '--port', str(database['PORT']),
        '--username', str(database['USER']),
        '--dbname', str(database['NAME']),
    ]


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as backup_file:
        for chunk in iter(lambda: backup_file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def create_postgres_backup(database, output_path):
    database = _postgres_configuration(database)
    output_path = Path(output_path).resolve()
    if output_path.exists():
        raise CommandError(f'Refusing to overwrite existing backup: {output_path}')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(
        f'.{output_path.name}.{uuid.uuid4().hex}.partial'
    )
    command = [
        'pg_dump',
        '--format=custom',
        '--no-owner',
        '--no-privileges',
        *_connection_arguments(database),
        '--file', str(temporary_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=_postgres_environment(database),
        )
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise CommandError('pg_dump completed without creating a usable backup.')
        os.replace(temporary_path, output_path)
        try:
            output_path.chmod(0o600)
        except OSError:
            pass
        checksum = file_sha256(output_path)
        checksum_path = output_path.with_suffix(f'{output_path.suffix}.sha256')
        checksum_path.write_text(f'{checksum}  {output_path.name}\n', encoding='utf-8')
        return output_path, checksum_path, checksum
    except (subprocess.CalledProcessError, OSError) as exc:
        raise CommandError('PostgreSQL backup failed. Review the service logs.') from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def verify_postgres_backup(backup_path):
    backup_path = Path(backup_path).resolve()
    if not backup_path.is_file():
        raise CommandError(f'Backup does not exist: {backup_path}')
    checksum_path = backup_path.with_suffix(f'{backup_path.suffix}.sha256')
    if not checksum_path.is_file():
        raise CommandError(f'Backup checksum does not exist: {checksum_path}')
    expected_checksum = checksum_path.read_text(encoding='utf-8').split()[0]
    actual_checksum = file_sha256(backup_path)
    if not hmac.compare_digest(expected_checksum, actual_checksum):
        raise CommandError('Backup checksum verification failed.')
    try:
        result = subprocess.run(
            ['pg_restore', '--list', str(backup_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise CommandError('PostgreSQL could not read the backup archive.') from exc
    return len([line for line in result.stdout.splitlines() if line and not line.startswith(';')])


def restore_postgres_backup(database, backup_path):
    database = _postgres_configuration(database)
    verify_postgres_backup(backup_path)
    command = [
        'pg_restore',
        '--clean',
        '--if-exists',
        '--no-owner',
        '--no-privileges',
        '--exit-on-error',
        '--single-transaction',
        *_connection_arguments(database),
        str(Path(backup_path).resolve()),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            env=_postgres_environment(database),
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise CommandError('PostgreSQL restore failed. Review the service logs.') from exc
