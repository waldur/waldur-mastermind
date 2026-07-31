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
#
# prepare_threshold=None disables psycopg3 server-side prepared statements.
# psycopg3 caches prepared statements per backend connection; PgBouncer in
# transaction pooling mode reassigns connections between transactions, so a
# statement prepared on connection A is invisible on connection B — causing
# "prepared statement _pg3_N does not exist" errors under load.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.get("POSTGRESQL_NAME", "waldur"),
        "HOST": env.get("POSTGRESQL_HOST", "localhost"),
        "PORT": env.get("POSTGRESQL_PORT", "5432"),
        "USER": env.get("POSTGRESQL_USER", "waldur"),
        "PASSWORD": env.get("POSTGRESQL_PASSWORD", "waldur"),
        "OPTIONS": {
            "prepare_threshold": None,
        },
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
            "prepare_threshold": None,
        },
    }

CELERY_RESULT_BACKEND = f"db+postgresql+psycopg://{DATABASES['default']['USER']}:{DATABASES['default']['PASSWORD']}@{DATABASES['default']['HOST']}:{DATABASES['default']['PORT']}/{DATABASES['default']['NAME']}"

CELERY_DEFAULT_QUEUE_TYPE = "quorum"
# CELERY_BROKER_TRANSPORT_OPTIONS now lives in
# ``waldur_core/server/celery_settings.py`` with the full dict
# (confirm_publish + heartbeat + socket_settings). Assigning here would
# replace the dict wholesale and silently strip the resilience knobs.

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
sentry_tasks_sample_rate = float(
    env.get("SENTRY_TASKS_SAMPLE_RATE", sentry_traces_sample_rate)
)
sentry_profiles_sample_rate = float(env.get("SENTRY_PROFILES_SAMPLE_RATE", 0))
sentry_environment = env.get("SENTRY_ENVIRONMENT") or env.get("WALDUR_ENVIRONMENT")


def _optional_int(name, default):
    value = env.get(name, default)
    if value in (None, "", "none", "null"):
        return None
    return int(value)


# Sentry Crons tolerances. sentry-sdk leaves all of these unset, which means a
# single late check-in immediately opens an issue. See
# waldur_core.server.sentry_crons for why they cannot be set in the Sentry UI.
sentry_crons_defaults = {
    # Minutes a check-in may be late before the window counts as missed.
    "checkin_margin": _optional_int("SENTRY_CRONS_CHECKIN_MARGIN", 5),
    # Consecutive failures before an issue is opened.
    "failure_issue_threshold": _optional_int("SENTRY_CRONS_FAILURE_ISSUE_THRESHOLD", 3),
    # Consecutive successes before the issue is resolved again.
    "recovery_threshold": _optional_int("SENTRY_CRONS_RECOVERY_THRESHOLD", 2),
    # Minutes a task may run before it is treated as stuck; unset by default
    # because Waldur's background tasks vary by orders of magnitude.
    "max_runtime": _optional_int("SENTRY_CRONS_MAX_RUNTIME", None),
}

if sentry_dsn:
    import importlib

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    from waldur_core.logging.sentry import before_send as sentry_before_send
    from waldur_core.server.sentry_crons import (
        build_monitor_config_defaults,
        patch_monitor_config_defaults,
    )

    def _traces_sampler(sampling_context):
        # Celery beat tasks run orders of magnitude less often than web
        # requests; sample them on a separate dial so they remain visible
        # without inflating overall trace volume.
        op = sampling_context.get("transaction_context", {}).get("op", "")
        if op == "queue.task.celery":
            return sentry_tasks_sample_rate
        return sentry_traces_sample_rate

    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[
            DjangoIntegration(middleware_spans=False),
            # monitor_beat_tasks emits Sentry Crons check-ins for every
            # celery-beat-scheduled task so silently skipped or stuck
            # background jobs surface as missed check-ins.
            CeleryIntegration(monitor_beat_tasks=True),
        ],
        # structlog defers rendering to the log handler, so the record message
        # sentry-sdk reads is the repr of the event dict. Without this hook the
        # variable data inside it splits one bug across many issue groups.
        before_send=sentry_before_send,
        # https://docs.sentry.io/platforms/python/guides/django/performance/
        traces_sampler=_traces_sampler,
        # Profiles fill the gap between instrumented spans and wall-clock
        # task duration; required to see where time is spent inside Python
        # code that has no explicit Sentry span (e.g. signal cascades).
        profiles_sample_rate=sentry_profiles_sample_rate,
        environment=sentry_environment,
        release="waldur-mastermind@" + importlib.metadata.version("waldur-mastermind"),
    )

    patch_monitor_config_defaults(
        build_monitor_config_defaults(**sentry_crons_defaults)
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

# Give this deployment its own Sentry Crons monitors. sentry-sdk keys monitors on
# the beat entry name, which is identical in every Waldur installation, so
# without this every deployment sharing a Sentry project collides on one monitor
# and each one's downtime is reported against all the others. Applied last so
# that entries contributed by extension config files are covered too.
if sentry_dsn and env.get("SENTRY_CRONS_SCOPE_MONITORS", "true").lower() == "true":
    from waldur_core.server.sentry_crons import scope_beat_schedule  # noqa: E402

    CELERY_BEAT_SCHEDULE = scope_beat_schedule(CELERY_BEAT_SCHEDULE, sentry_environment)

if not SECRET_KEY:
    raise Exception("GLOBAL_SECRET_KEY is not set")
