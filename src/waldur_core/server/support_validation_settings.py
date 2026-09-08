"""Settings module for the support-assistant validation harness.

Self-contained postgres + locmem cache + memory celery so the harness can
run without external services beyond the LLM endpoint, which is read from
the environment rather than from the database.

Usage:
    export AI_ASSISTANT_API_URL=https://your-llm-endpoint
    export AI_ASSISTANT_API_TOKEN=sk-...
    export AI_ASSISTANT_MODEL=qwen3.5-122b-nonthinking

    DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
        waldur ai_assistant test_evaluation --preset credit_realistic \\
            --wipe-database waldur_support_validation
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

# Constance in memory, seeded from the environment. The validation database
# is reloaded from demo presets, so config kept in it is neither durable nor
# reviewable — and a token written to a settings-selected database is one
# that can be written to the wrong one. Nothing here is read from or
# persisted to any database.
CONSTANCE_BACKEND = "constance.backends.memory.MemoryBackend"


def _constance_default(key, value):
    _, *rest = CONSTANCE_CONFIG[key]  # noqa: F405
    CONSTANCE_CONFIG[key] = (value, *rest)  # noqa: F405


# The harness exists to exercise the assistant, so it is always on here.
_constance_default("AI_ASSISTANT_ENABLED", True)

for _key in (
    "AI_ASSISTANT_API_URL",
    "AI_ASSISTANT_API_TOKEN",
    "AI_ASSISTANT_MODEL",
    "AI_ASSISTANT_BACKEND_TYPE",
):
    _env_value = os.environ.get(_key)
    if _env_value:
        _constance_default(_key, _env_value)
