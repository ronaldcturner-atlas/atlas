import logging
import re
import time
import uuid

from django.conf import settings


logger = logging.getLogger('atlas.request')
REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
QUIET_PATHS = {'/api/health/', '/api/ready/'}


class RequestObservabilityMiddleware:
    """Attach a request ID and emit safe request metadata after each response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied_request_id = request.headers.get('X-Request-ID', '')
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else uuid.uuid4().hex
        )
        request.atlas_request_id = request_id
        started_at = time.monotonic()
        response = self.get_response(request)
        duration_ms = round((time.monotonic() - started_at) * 1000, 1)
        response['X-Request-ID'] = request_id

        status_code = response.status_code
        slow_request_ms = getattr(settings, 'ATLAS_SLOW_REQUEST_MS', 1000)
        should_log = (
            getattr(settings, 'ATLAS_REQUEST_LOGGING', False)
            and (request.path not in QUIET_PATHS or status_code >= 400)
        )
        if should_log:
            level = logging.INFO
            if status_code >= 500:
                level = logging.ERROR
            elif status_code >= 400 or duration_ms >= slow_request_ms:
                level = logging.WARNING
            user = getattr(request, 'user', None)
            logger.log(
                level,
                'HTTP request completed.',
                extra={
                    'event': 'http.request',
                    'request_id': request_id,
                    'method': request.method,
                    'path': request.path,
                    'status_code': status_code,
                    'duration_ms': duration_ms,
                    'user_id': (
                        user.pk
                        if user is not None and getattr(user, 'is_authenticated', False)
                        else None
                    ),
                },
            )
        return response
