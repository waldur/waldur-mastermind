"""Settings module for the support-assistant validation harness.

Self-contained postgres + locmem cache + memory celery so the harness can
run without external services beyond the LLM endpoint pointed at by
Constance (AI_ASSISTANT_API_URL / TOKEN / MODEL).

Usage:
    DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
        python scripts/support_validation_run.py
"""

import os

from waldur_core.server.base_settings import *  # noqa

SECRET_KEY = "support-validation-only-key"  # noqa: S105

DEBUG = True

MEDIA_ROOT = "/tmp/"  # noqa: S108

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("WALDUR_VALIDATION_DB", "waldur_support_validation"),
    },
}

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "support-validation-cache",
    }
}

CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = True

# Disable DRF throttling so the matrix doesn't get 429-ed on rapid POSTs.
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # noqa: F405
