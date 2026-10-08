import logging
import os

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse


logger = logging.getLogger(__name__)


def _release_identifier():
    release = (
        os.environ.get('ATLAS_RELEASE')
        or os.environ.get('RAILWAY_GIT_COMMIT_SHA')
        or 'development'
    )
    return release[:12]


def health(request):
    return JsonResponse(
        {
            "status": "ok",
            "service": "atlas-backend",
            "release": _release_identifier(),
        }
    )


def readiness(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        executor = MigrationExecutor(connection)
        pending_migrations = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:
        logger.exception('Atlas readiness check failed.')
        return JsonResponse(
            {
                'status': 'unavailable',
                'checks': {'database': 'unavailable'},
            },
            status=503,
        )

    if pending_migrations:
        return JsonResponse(
            {
                'status': 'unavailable',
                'checks': {
                    'database': 'ok',
                    'migrations': 'pending',
                },
            },
            status=503,
        )

    return JsonResponse({
        'status': 'ready',
        'checks': {
            'database': 'ok',
            'migrations': 'current',
        },
    })
