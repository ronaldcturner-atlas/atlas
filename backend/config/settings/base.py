import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name, default=''):
    return [value.strip() for value in os.environ.get(name, default).split(',') if value.strip()]


def env_int(name, default, minimum=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(f'{name} must be a whole number.') from exc
    if minimum is not None and value < minimum:
        raise ImproperlyConfigured(f'{name} must be at least {minimum}.')
    return value


SECRET_KEY = os.environ.get("SECRET_KEY", "unsafe-secret-key")
DEBUG = env_bool("DEBUG", False)

# Kept at one until run-isolation tests pass; deployment can then explicitly
# raise it without another schema change.
OPTIMIZER_MAX_CONCURRENT_RUNS_PER_VERSION = int(
    os.environ.get("OPTIMIZER_MAX_CONCURRENT_RUNS_PER_VERSION", "1")
)
OPTIMIZER_ENABLE_PARALLEL_ISOLATION = (
    env_bool("OPTIMIZER_ENABLE_PARALLEL_ISOLATION", False)
)
ATLAS_V2_ENABLED = (
    os.environ.get(
        "ATLAS_V2_ENABLED",
        os.environ.get("ATLAS_V2_TEST_ENABLED", "False"),
    ).strip().lower() in {'1', 'true', 'yes', 'on'}
)
# Compatibility setting for older deployments during the product transition.
ATLAS_V2_TEST_ENABLED = ATLAS_V2_ENABLED

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")

ATLAS_ENABLE_DEVELOPMENT_ROLE_TEST = env_bool(
    "ATLAS_ENABLE_DEVELOPMENT_ROLE_TEST",
    DEBUG,
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "apps.common",
    "apps.accounts",
    "apps.organizations",
    "apps.domains",
    "apps.facilities",
    "apps.scheduling",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "apps.common.middleware.RequestObservabilityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.domains.middleware.DevelopmentRoleTestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "atlas"),
        "USER": os.environ.get("DB_USER", "atlas"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "atlas"),
        "HOST": os.environ.get("DB_HOST", "postgres"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# CORS Configuration
LOCAL_FRONTEND_ORIGINS = (
    "http://localhost:5173,http://localhost:5174,"
    "http://127.0.0.1:5173,http://127.0.0.1:5174"
)
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", LOCAL_FRONTEND_ORIGINS)

# Allow credentials in CORS requests (needed for session auth)
CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = ["X-Request-ID"]

ATLAS_SLOW_REQUEST_MS = env_int("ATLAS_SLOW_REQUEST_MS", 1000, minimum=1)
ATLAS_REQUEST_LOGGING = env_bool("ATLAS_REQUEST_LOGGING", False)

# CSRF Configuration - trust these origins for cross-origin requests
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", LOCAL_FRONTEND_ORIGINS)

# REST Framework Configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login_ip": os.environ.get("LOGIN_IP_RATE", "20/min"),
        "login_account": os.environ.get("LOGIN_ACCOUNT_RATE", "5/min"),
    },
}
