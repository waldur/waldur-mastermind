"""
Django base settings for Waldur Core.
"""

import locale

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
import os
from datetime import timedelta

from waldur_core.core import WaldurExtension
from waldur_core.core.metadata import WaldurConfiguration
from waldur_core.server.admin.settings import *
from waldur_core.server.celery_settings import *
from waldur_core.server.constance_settings import *
from waldur_core.server.openapi_settings import *

encoding = locale.getpreferredencoding()
if encoding.lower() != "utf-8":
    raise Exception(
        f"""Your system's preferred encoding is `{encoding}`, but Waldur requires `UTF-8`.
Fix it by setting the LC_* and LANG environment settings. Example:
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
"""
    )

ADMINS = ()

BASE_DIR = os.path.abspath(
    os.path.join(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".."), "..")
)

DEBUG = False

MEDIA_ROOT = "/media_root/"

MEDIA_URL = "/media/"

ALLOWED_HOSTS = []

# Application definition
INSTALLED_APPS = (
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.humanize",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "waldur_core.landing",
    "waldur_core.core",
    "waldur_core.permissions",
    "waldur_core.quotas",
    "waldur_core.structure",
    "waldur_core.onboarding",
    "waldur_core.users",
    "waldur_core.media",
    "waldur_core.logging",
    "waldur_core.checklist",
    "waldur_core.user_actions",
    "waldur_core.passkeys",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "axes",
    "django_structlog",
    "django_fsm",
    "reversion",
    "jsoneditor",
    "modeltranslation",
    "health_check",
    "health_check.db",
    "health_check.cache",
    "health_check.storage",
    "health_check.contrib.migrations",
    # Note: We use waldur_core.core.health_checks.CeleryWorkersHealthCheck instead of
    # health_check.contrib.celery_ping for better performance (connection pooling + targeted pings)
    "netfields",
    "constance",
    "constance.backends.database",
    "drf_spectacular",
)
INSTALLED_APPS += ADMIN_INSTALLED_APPS  # noqa: F405

MIDDLEWARE = (
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "waldur_core.server.middleware.cors_middleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "waldur_core.logging.middleware.CaptureEventContextMiddleware",
    "django_structlog.middlewares.RequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "waldur_core.server.middleware.ImpersonationMiddleware",
    "axes.middleware.AxesMiddleware",
)

REST_FRAMEWORK = {
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "waldur_core.core.authentication.ImpersonationAuthentication",
        "waldur_core.core.authentication.SessionAuthentication",
        "waldur_core.core.authentication.PATAuthentication",
        "waldur_core.core.authentication.OIDCAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PARSER_CLASSES": [
        "drf_orjson_renderer.parsers.ORJSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_RENDERER_CLASSES": (
        "waldur_core.core.renderers.WaldurORJSONRenderer",
        "waldur_core.core.renderers.BrowsableAPIRenderer",
    ),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "oauth": "10/s",
        # api-auth/default/init/. The probe runs on every anonymous landing on
        # the portal root and writes nothing, so it is budgeted for a lecture
        # hall arriving behind one NAT at once; the navigation is a sign-in
        # attempt and does write session state. Both are per client address.
        "oauth_probe": "120/s",
        "oauth_default": "60/s",
        "token_exchange": "60/min",
        "matrix_credentials": "1000/hour",
        "matrix_webhook": "10000/hour",
        # Passkey ceremonies. Sign-in is anonymous and unauthenticated, so it
        # is the tighter of the two. Deliberately not wired into django-axes:
        # a counter shared with password login would let an attacker lock a
        # user out of password auth simply by grinding assertions.
        "passkey_signin": "30/min",
        "passkey_registration": "20/min",
        # Guards the staff-only mail diagnostics: the probe opens a socket to a
        # third-party relay and the test send delivers a real message, so both
        # are cheap to abuse and rare in legitimate use.
        "email_diagnostics": "20/hour",
    },
    "DEFAULT_PAGINATION_CLASS": "waldur_core.core.pagination.LinkHeaderPagination",
    "DEFAULT_SCHEMA_CLASS": "waldur_core.core.openapi_inspector.WaldurOpenApiInspector",
    "PAGE_SIZE": 10,
    "EXCEPTION_HANDLER": "waldur_core.core.views.exception_handler",
    # Return native `Date` and `Time` objects in `serializer.data`
    "DATETIME_FORMAT": None,
    "DATE_FORMAT": None,
    "TIME_FORMAT": None,
    "ORDERING_PARAM": "o",
}

AUTHENTICATION_BACKENDS = (
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
    "waldur_core.core.authentication.AdminAuthenticationBackend",
)

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

ANONYMOUS_USER_ID = None

CONTEXT_PROCESSORS = (
    "django.template.context_processors.debug",
    "django.template.context_processors.request",
    "django.contrib.auth.context_processors.auth",
    "django.contrib.messages.context_processors.messages",
    "django.template.context_processors.i18n",
    "django.template.context_processors.media",
    "django.template.context_processors.static",
    "django.template.context_processors.tz",
)

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": (os.path.join(BASE_DIR, "src", "waldur_core", "templates"),),
        "OPTIONS": {
            "context_processors": CONTEXT_PROCESSORS,
            "loaders": (
                "waldur_core.core.template_loaders.DatabaseTemplateLoader",
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ),
        },
    },
]

ROOT_URLCONF = "waldur_core.server.urls"

AUTH_USER_MODEL = "core.User"

# Session
# https://docs.djangoproject.com/en/4.2/ref/settings/#sessions
SESSION_COOKIE_AGE = 3600
SESSION_SAVE_EVERY_REQUEST = True

WSGI_APPLICATION = "waldur_core.server.wsgi.application"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/
STATIC_URL = "/static/"

# RabbitMQ requirements:
# rabbitmq-plugins enable rabbitmq_stomp
# rabbitmq-plugins enable rabbitmq_web_stomp (for websockets)
RABBITMQ = {
    "HOST": "localhost",
    "STOMP_PORT": 61613,
    "USER": "test",
    "PASSWORD": "test",
    "MANAGEMENT_PORT": 15672,
}

globals().update(WaldurConfiguration().dict())

# Field-level encryption at rest (see docs/resource-api-keys.md).
# FIELD_ENCRYPTION_KEY is the primary Fernet key used to encrypt/decrypt secret
# columns. It is deliberately a separate setting from SECRET_KEY: leaking Django
# settings must not, by itself, unlock encrypted DB fields.
FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "")
# Rotating the encryption key: to replace FIELD_ENCRYPTION_KEY, set the new key
# as the primary and move the OLD key(s) here (comma-separated). New writes use
# the primary; reads still succeed against any fallback, so existing rows stay
# readable without a re-encrypt migration. Once every row has been re-written
# under the new primary, the old key can be dropped from this list.
FIELD_ENCRYPTION_KEY_FALLBACKS = [
    key
    for key in os.environ.get("FIELD_ENCRYPTION_KEY_FALLBACKS", "").split(",")
    if key
]

for ext in WaldurExtension.get_extensions():
    INSTALLED_APPS += (ext.django_app(),)

    for key, val in ext.Settings.__dict__.items():
        if not key.startswith("_"):
            globals()[key] = val

    ext.update_settings(globals())

AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_COOLOFF_TIME = timedelta(minutes=10)
AXES_FAILURE_LIMIT = 5
# By default django-axes masks username and ip_address in logs, making them useless
# for security monitoring. Override to show actual values in login failure logs.
AXES_SENSITIVE_PARAMETERS = []


STORAGES = {
    "default": {
        "BACKEND": "waldur_core.media.storage.DatabaseStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Disable excessive xmlschema logging
import logging

import structlog

logging.getLogger("xmlschema").propagate = False

# Disable excessive Celery task registration logging
logging.getLogger("celery.utils.imports").setLevel(logging.WARNING)
logging.getLogger("celery.app.autodiscover").setLevel(logging.WARNING)

# Processors for stdlib loggers (foreign_pre_chain) - ExtraAdder merges record.extra
_FOREIGN_PRE_CHAIN = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.ExtraAdder(),
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.format_exc_info,
]

# Use JSON in production, readable console in development
_USE_JSON_LOGS = os.environ.get("WALDUR_DEV_LOGS", "").lower() not in (
    "1",
    "true",
    "yes",
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structlog_console": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.dev.ConsoleRenderer(),
            "foreign_pre_chain": _FOREIGN_PRE_CHAIN,
        },
        "structlog_json": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.processors.JSONRenderer(),
            "foreign_pre_chain": _FOREIGN_PRE_CHAIN,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structlog_json" if _USE_JSON_LOGS else "structlog_console",
        },
        "database": {
            "class": "waldur_core.logging.log.DatabaseLogHandler",
            "level": "INFO",
            "formatter": "structlog_json",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console", "database"],
    },
    "loggers": {
        # Override Django's DEFAULT_LOGGING to use structlog formatter.
        # Without this, DEFAULT_LOGGING creates a plain StreamHandler on
        # the "django" logger, causing duplicate unstructured output for
        # django.request and other django.* loggers.
        "django": {
            "level": "INFO",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        # Django's dev server logger. DEFAULT_LOGGING gives it a
        # ServerFormatter handler producing "[timestamp] GET /..." lines.
        # Override to use structlog instead.
        "django.server": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        "django_structlog": {
            "level": "WARNING",
        },
        # django-axes logs login attempts with an "AXES:" prefix.
        # Route through structlog for consistent JSON output.
        "axes": {
            "level": "WARNING",
            "handlers": ["console"],
            "propagate": False,
        },
        # python-neutronclient emits a deprecation notice on every client
        # init ("deprecated in favor of OpenstackSDK"). We still depend on
        # it, so suppress the per-call noise until the migration lands.
        "neutronclient": {
            "level": "ERROR",
        },
    },
}

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

DJANGO_STRUCTLOG_CELERY_ENABLED = True

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

LANGUAGES = (
    ("en", "English"),
    ("et", "Eesti"),
    ("lt", "Lietuvių"),
    ("lv", "Latviešu"),
    ("ru", "Русский"),
    ("it", "Italiano"),
    ("de", "Deutsch"),
    ("da", "Dansk"),
    ("sv", "Svenska"),
    ("es", "Español"),
    ("fr", "Français"),
    ("nb", "Norsk"),
    ("ar", "العربية"),
    ("cs", "Čeština"),
    ("hr", "Hrvatski"),
    ("sl", "Slovenščina"),
    ("el", "Ελληνικά"),
    ("bg", "Български"),
    ("km", "ខ្មែរ"),
    ("mk", "Македонски"),
    ("sq", "Shqip"),
)

# Disable SAML2 CSP warnings
SAML_CSP_HANDLER = ""
