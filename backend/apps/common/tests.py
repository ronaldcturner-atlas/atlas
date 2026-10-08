import json
import logging
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase, override_settings

from .logging import JsonFormatter


class CommonTests(TestCase):
    def test_health_endpoint(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "ok",
            "service": "atlas-backend",
            "release": "development",
        })

    def test_readiness_confirms_database_and_migrations(self):
        response = self.client.get('/api/ready/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'status': 'ready',
            'checks': {
                'database': 'ok',
                'migrations': 'current',
            },
        })

    @patch('apps.common.api.logger.exception')
    @patch('apps.common.api.connection.cursor', side_effect=OperationalError)
    def test_readiness_returns_503_without_exposing_database_details(self, cursor, log_exception):
        response = self.client.get('/api/ready/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            'status': 'unavailable',
            'checks': {'database': 'unavailable'},
        })

    @patch('apps.common.api.MigrationExecutor')
    def test_readiness_returns_503_when_migrations_are_pending(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = ['latest']
        executor.migration_plan.return_value = [('migration', False)]

        response = self.client.get('/api/ready/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            'status': 'unavailable',
            'checks': {
                'database': 'ok',
                'migrations': 'pending',
            },
        })

    def test_every_response_has_a_safe_request_identifier(self):
        response = self.client.get('/api/health/')

        self.assertRegex(response['X-Request-ID'], r'^[a-f0-9]{32}$')

        supplied = self.client.get(
            '/api/health/',
            HTTP_X_REQUEST_ID='browser-request_123',
        )
        self.assertEqual(supplied['X-Request-ID'], 'browser-request_123')

        rejected = self.client.get(
            '/api/health/',
            HTTP_X_REQUEST_ID='unsafe request id',
        )
        self.assertNotEqual(rejected['X-Request-ID'], 'unsafe request id')

    def test_json_logs_include_diagnostics_but_ignore_sensitive_extras(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name='atlas.test',
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg='Request failed.',
            args=(),
            exc_info=None,
        )
        record.event = 'http.request'
        record.request_id = 'request_123'
        record.status_code = 500
        record.password = 'must-not-appear'
        record.session_cookie = 'must-not-appear'

        payload = json.loads(formatter.format(record))

        self.assertEqual(payload['request_id'], 'request_123')
        self.assertEqual(payload['status_code'], 500)
        self.assertNotIn('password', payload)
        self.assertNotIn('session_cookie', payload)

    @override_settings(ATLAS_REQUEST_LOGGING=True)
    def test_request_log_omits_query_string(self):
        with self.assertLogs('atlas.request', level='WARNING') as captured:
            response = self.client.get('/missing/?token=must-not-appear')

        self.assertEqual(response.status_code, 404)
        record = captured.records[0]
        self.assertEqual(record.path, '/missing/')
        self.assertNotIn('must-not-appear', record.getMessage())
