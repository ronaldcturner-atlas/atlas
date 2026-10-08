import os
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

from .base import *
from .base import env_bool, env_list


DEBUG = False
ATLAS_ENABLE_DEVELOPMENT_ROLE_TEST = False
ATLAS_ALLOW_SELF_SERVICE_ORGANIZATION_BOOTSTRAP = False
REJECT_SHARED_TEST_PASSWORD = True
# Historical V1 results remain readable, but production users may only launch
# the V2 optimizer. V1-derived construction/scoring internals are still shipped
# because V2 currently reuses those safety-critical components.
ATLAS_LEGACY_V1_LAUNCH_ENABLED = False


def required_environment_value(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise ImproperlyConfigured(f'{name} must be set in production.')
    return value


SECRET_KEY = required_environment_value('SECRET_KEY')
if SECRET_KEY == 'unsafe-secret-key' or len(SECRET_KEY) < 50:
    raise ImproperlyConfigured('SECRET_KEY must be a strong, unique value of at least 50 characters.')

railway_public_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '').strip()
railway_private_domain = os.environ.get('RAILWAY_PRIVATE_DOMAIN', '').strip()
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS')
for railway_domain in (railway_public_domain, railway_private_domain):
    if railway_domain and railway_domain not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(railway_domain)
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured('ALLOWED_HOSTS or RAILWAY_PUBLIC_DOMAIN must be set in production.')
if '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured('ALLOWED_HOSTS cannot contain * in production.')

frontend_origins = env_list('FRONTEND_ORIGINS')
CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS') or frontend_origins
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS') or frontend_origins
if railway_public_domain:
    backend_origin = f'https://{railway_public_domain}'
    if backend_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(backend_origin)


def database_from_url(database_url):
    parsed = urlparse(database_url)
    if parsed.scheme not in {'postgres', 'postgresql'}:
        raise ImproperlyConfigured('DATABASE_URL must use PostgreSQL.')
    query = parse_qs(parsed.query)
    configuration = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': unquote(parsed.path.lstrip('/')),
        'USER': unquote(parsed.username or ''),
        'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname or '',
        'PORT': str(parsed.port or 5432),
        'CONN_MAX_AGE': 600,
    }
    ssl_mode = query.get('sslmode', [None])[0]
    if ssl_mode:
        configuration['OPTIONS'] = {'sslmode': ssl_mode}
    if not all(configuration[key] for key in ('NAME', 'USER', 'PASSWORD', 'HOST')):
        raise ImproperlyConfigured('DATABASE_URL is incomplete.')
    return configuration


database_url = os.environ.get('DATABASE_URL', '').strip()
if database_url:
    DATABASES = {'default': database_from_url(database_url)}
else:
    required_database_values = {
        name: required_environment_value(name)
        for name in ('DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST')
    }
    DATABASES['default'].update({
        'NAME': required_database_values['DB_NAME'],
        'USER': required_database_values['DB_USER'],
        'PASSWORD': required_database_values['DB_PASSWORD'],
        'HOST': required_database_values['DB_HOST'],
        'PORT': os.environ.get('DB_PORT', '5432'),
        'CONN_MAX_AGE': 600,
    })

if DATABASES['default']['PASSWORD'] == 'atlas':
    raise ImproperlyConfigured(
        'The database password must not use the development default in production.'
    )

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r'^api/health/$', r'^api/ready/$']
try:
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '3600'))
except ValueError as exc:
    raise ImproperlyConfigured('SECURE_HSTS_SECONDS must be a whole number.') from exc
if SECURE_HSTS_SECONDS < 0:
    raise ImproperlyConfigured('SECURE_HSTS_SECONDS cannot be negative.')
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

SESSION_COOKIE_NAME = 'atlas_sessionid'
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax').strip().capitalize()
if SESSION_COOKIE_SAMESITE not in {'Lax', 'Strict', 'None'}:
    raise ImproperlyConfigured('SESSION_COOKIE_SAMESITE must be Lax, Strict, or None.')
try:
    SESSION_COOKIE_AGE = int(os.environ.get('SESSION_COOKIE_AGE_SECONDS', '43200'))
except ValueError as exc:
    raise ImproperlyConfigured('SESSION_COOKIE_AGE_SECONDS must be a whole number.') from exc
if SESSION_COOKIE_AGE <= 0:
    raise ImproperlyConfigured('SESSION_COOKIE_AGE_SECONDS must be greater than zero.')
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool('SESSION_EXPIRE_AT_BROWSER_CLOSE', False)

CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE
ATLAS_REQUEST_LOGGING = env_bool('ATLAS_REQUEST_LOGGING', True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'apps.common.logging.JsonFormatter',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.environ.get('LOG_LEVEL', 'INFO').upper(),
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['console'],
            'level': os.environ.get('LOG_LEVEL', 'INFO').upper(),
            'propagate': False,
        },
    },
}
