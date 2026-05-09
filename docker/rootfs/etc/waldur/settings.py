# Django settings for Waldur
import os

from waldur_core.server.base_settings import *

BASE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "..")
)
TEMPLATES[0]["DIRS"] = [os.path.join(BASE_DIR, "waldur_core", "templates")]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

env: dict = os.environ

conf_dir = env.get("WALDUR_BASE_CONFIG_DIR", "/etc/waldur")
data_dir = "/usr/share/waldur"
work_dir = "/var/lib/waldur"
templates_dir = os.path.join(conf_dir, "templates")

SECRET_KEY = env.get("GLOBAL_SECRET_KEY")

media_root: str = os.path.join(work_dir, "media")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env.get("GLOBAL_DEBUG", "false").lower() == "true"

for tmpl in TEMPLATES:
    tmpl.setdefault("OPTIONS", {})
    tmpl["OPTIONS"]["debug"] = DEBUG

# Allow to overwrite templates
TEMPLATES[0]["DIRS"].insert(0, templates_dir)

# For security reason disable browsable API rendering in production
if not DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = tuple(
        renderer
        for renderer in REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]
        if renderer != "waldur_core.core.renderers.BrowsableAPIRenderer"
    )

MEDIA_ROOT = media_root

ALLOWED_HOSTS = ["*"]

# See also: https://docs.djangoproject.com/en/4.2/ref/settings/#databases
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.get("POSTGRESQL_NAME", "waldur"),
        "HOST": env.get("POSTGRESQL_HOST", "localhost"),
        "PORT": env.get("POSTGRESQL_PORT", "5432"),
        "USER": env.get("POSTGRESQL_USER", "waldur"),
        "PASSWORD": env.get("POSTGRESQL_PASSWORD", "waldur"),
    },
}

if env.get("POSTGRESQL_READONLY_USER"):
    DATABASES["readonly"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.get("POSTGRESQL_NAME", "waldur"),
        "HOST": env.get("POSTGRESQL_HOST", "localhost"),
        "PORT": env.get("POSTGRESQL_PORT", "5432"),
        "USER": env.get("POSTGRESQL_READONLY_USER"),
        "PASSWORD": env.get("POSTGRESQL_READONLY_PASSWORD"),
        "OPTIONS": {
            "target_session_attrs": "read-only",
        },
    }

CELERY_RESULT_BACKEND = f"db+postgresql+psycopg://{DATABASES['default']['USER']}:{DATABASES['default']['PASSWORD']}@{DATABASES['default']['HOST']}:{DATABASES['default']['PORT']}/{DATABASES['default']['NAME']}"

CELERY_DEFAULT_QUEUE_TYPE = "quorum"
CELERY_BROKER_TRANSPORT_OPTIONS = {"confirm_publish": True}

# Static files
# See also: https://docs.djangoproject.com/en/4.2/ref/settings/#static-files
STATIC_ROOT = env.get("GLOBAL_STATIC_ROOT", os.path.join(data_dir, "static"))

# Email
# See also: https://docs.djangoproject.com/en/4.2/ref/settings/#default-from-email
default_from_email = env.get("GLOBAL_DEFAULT_FROM_EMAIL")
if default_from_email:
    DEFAULT_FROM_EMAIL = default_from_email

DEFAULT_REPLY_TO_EMAIL = env.get("GLOBAL_DEFAULT_REPLY_TO_EMAIL", "")
EMAIL_HOOK_FROM_EMAIL = env.get("GLOBAL_EMAIL_HOOK_FROM_EMAIL", "")

# Session
# https://docs.djangoproject.com/en/4.2/ref/settings/#sessions
SESSION_COOKIE_AGE = env.get("AUTH_COOKIE_AGE", 3600)

# Waldur Core internal configuration
# See also: http://docs.waldur.com/latest/
token_lifetime = env.get("AUTH_TOKEN_LIFETIME", 3600)
WALDUR_CORE.update(
    {
        "TOKEN_LIFETIME": timedelta(seconds=token_lifetime),
    }
)

# Sentry integration
# See also: https://docs.sentry.io/platforms/python/guides/django/
sentry_dsn = env.get("SENTRY_DSN")
sentry_traces_sample_rate = float(env.get("SENTRY_TRACES_SAMPLE_RATE", 0.01))

if sentry_dsn:
    import importlib

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        # https://docs.sentry.io/platforms/python/guides/django/performance/
        traces_sample_rate=sentry_traces_sample_rate,
        release="waldur-mastermind@" + importlib.metadata.version("waldur-mastermind"),
    )

    WALDUR_CORE["HOMEPORT_SENTRY_TRACES_SAMPLE_RATE"] = sentry_traces_sample_rate

# Additional configuration files for Waldur
# 'override.conf.py' must be the first element to override settings in core.ini but not plugin configuration.
# Plugin configuration files must me ordered alphabetically to provide predictable configuration handling order.
extensions = ("override.conf.py", "logging.conf.py", "saml2.conf.py")
for extension_name in extensions:
    # optionally load extension configurations
    extension_conf_file_path = os.path.join(conf_dir, extension_name)
    if os.path.isfile(extension_conf_file_path):
        try:
            exec(  # nosec
                compile(
                    open(extension_conf_file_path, encoding="utf-8").read(),
                    extension_conf_file_path,
                    "exec",
                )
            )
        except Exception as e:
            raise type(e)(
                f"Error loading extension config '{extension_conf_file_path}': {e}"
            ) from e

# Re-apply SAML2 extension settings so that values mutated by override.conf.py /
# saml2.conf.py (e.g. WALDUR_AUTH_SAML2 or WALDUR_CORE['MASTERMIND_URL']) propagate
# into the derived SAML_CONFIG. SAML2Extension.update_settings() is idempotent.
# See gh-83.
from waldur_auth_saml2.extension import SAML2Extension as _SAML2Extension  # noqa: E402

_SAML2Extension.update_settings(globals())

if not SECRET_KEY:
    raise Exception("GLOBAL_SECRET_KEY is not set")
