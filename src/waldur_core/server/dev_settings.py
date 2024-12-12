from waldur_core.server.base_settings import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
SECRET_KEY = "..."

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql_psycopg2",
        "NAME": "postgres",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "db",
        "PORT": "5432",
    }
}

STATIC_ROOT = "static"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "is-event": {
            "()": "waldur_core.logging.log.RequireEvent",
        },
        "is-not-event": {
            "()": "waldur_core.logging.log.RequireNotEvent",
        },
    },
    "formatters": {
        "message-only": {
            "format": "%(message)s",
        },
        "simple": {
            "format": "%(asctime)s %(levelname)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
}

DEFAULT_FROM_EMAIL = "noreply@example.com"
DEFAULT_REPLY_TO_EMAIL = "support@example.com"

INSTALLED_APPS += ("corsheaders",)  # noqa
MIDDLEWARE = ("corsheaders.middleware.CorsMiddleware",) + MIDDLEWARE  # noqa
CORS_ORIGIN_ALLOW_ALL = True
CORS_EXPOSE_HEADERS = (
    "link",
    "x-result-count",
    "x-impersonated-user-uuid",
)

from corsheaders.defaults import default_headers  # noqa

CORS_ALLOW_HEADERS = (
    *default_headers,
    "X-Impersonated-User-Uuid",
)

CACHES["default"]["LOCATION"] = "redis://redis:6379/1"  # noqa
CELERY_BROKER_URL = "redis://redis"
CELERY_RESULT_BACKEND = "redis://redis"
