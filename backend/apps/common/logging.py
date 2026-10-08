import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Small dependency-free JSON formatter for Railway's stdout collector."""

    EXTRA_FIELDS = (
        'event',
        'request_id',
        'method',
        'path',
        'status_code',
        'duration_ms',
        'user_id',
    )

    def format(self, record):
        payload = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        for field in self.EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)
