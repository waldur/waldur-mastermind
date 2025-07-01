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

from waldur_core.server.openapi_settings import *
from waldur_core.server.constance_settings import *
from waldur_core.server.celery_settings import *

encoding = locale.getpreferredencoding()
if encoding.lower() != "utf-8":
    raise Exception(
        """Your system's preferred encoding is `{}`, but Waldur requires `UTF-8`.
Fix it by setting the LC_* and LANG environment settings. Example:
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
""".format(encoding)
    )

ADMINS = ()

BASE_DIR = os.path.abspath(
    os.path.join(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".."), "..")
)

DEBUG = False

MEDIA_ROOT = "/media_root/"

MEDIA_URL = "/media/"

ALLOWED_HOSTS = []
SITE_ID = 1
DBTEMPLATES_USE_REVERSION = True
DBTEMPLATES_USE_CODEMIRROR = True

# Application definition
INSTALLED_APPS = (
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.humanize",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "waldur_core.landing",
    "waldur_core.core",
    "waldur_core.permissions",
    "waldur_core.quotas",
    "waldur_core.structure",
    "waldur_core.users",
    "waldur_core.media",
    "waldur_core.logging",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "axes",
    "django_fsm",
    "reversion",
    "jsoneditor",
    "modeltranslation",
    "health_check",
    "health_check.db",
    "health_check.cache",
    "health_check.storage",
    "health_check.contrib.migrations",
    "health_check.contrib.celery_ping",
    "dbtemplates",
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
        "drf_orjson_renderer.renderers.ORJSONRenderer",
        "waldur_core.core.renderers.BrowsableAPIRenderer",
    ),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "oauth": "10/s",
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
            "loaders": ADMIN_TEMPLATE_LOADERS
            + (
                "dbtemplates.loader.Loader",
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ),  # noqa: F405
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

USE_L10N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/
STATIC_URL = "/static/"

# RabbitMQ requirements:
# rabbitmq-plugins enable rabbitmq_mqtt
# rabbitmq-plugins enable rabbitmq_web_mqtt (for websockets)
RABBITMQ = {
    "HOST": "localhost",
    "MQTT_PORT": 1883,
    "STOMP_PORT": 61613,
    "USER": "test",
    "PASSWORD": "test",
    "MANAGEMENT_PORT": 15672,
}

globals().update(WaldurConfiguration().dict())

for ext in WaldurExtension.get_extensions():
    INSTALLED_APPS += (ext.django_app(),)

    for key, val in ext.Settings.__dict__.items():
        if not key.startswith("_"):
            globals()[key] = val

    ext.update_settings(globals())

AXES_LOCKOUT_PARAMETERS = ["username"]
AXES_COOLOFF_TIME = timedelta(minutes=10)
AXES_FAILURE_LIMIT = 5

DEFAULT_FILE_STORAGE = "waldur_core.media.storage.DatabaseStorage"

# Disable excessive xmlschema and django-axes logging
import logging

logging.getLogger("xmlschema").propagate = False
logging.getLogger("axes").propagate = False

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
)

# Disable SAML2 CSP warnings
SAML_CSP_HANDLER = ""
