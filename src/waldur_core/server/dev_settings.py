from waldur_core.server.base_settings import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
SECRET_KEY = "..."

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "postgres",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "db",
        "PORT": "5432",
    }
}

CELERY_BROKER_URL = "amqp://rabbitmq:rabbitmq@queue:5672"

CELERY_RESULT_BACKEND = f"db+postgresql+psycopg://{DATABASES['default']['USER']}:{DATABASES['default']['PASSWORD']}@{DATABASES['default']['HOST']}:{DATABASES['default']['PORT']}/{DATABASES['default']['NAME']}"

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
