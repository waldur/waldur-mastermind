# Django test settings for Waldur Core.
from waldur_core.server.base_settings import *  # noqa

SECRET_KEY = "test-key"

# Valid Fernet key so field encryption is exercised in tests. Test-only value.
FIELD_ENCRYPTION_KEY = "0_MF86u8HjafXHqQSf9jm5r0Rbhn_jOcwTHk1f-3OqY="

DEBUG = True

MEDIA_ROOT = "/tmp/"  # noqa: S108

INSTALLED_APPS += (  # noqa: F405
    "waldur_core.quotas.tests",
    "waldur_core.structure.tests",
    "waldur_pid.tests",
)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-snowflake",
    }
}

ROOT_URLCONF = "waldur_core.structure.tests.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "db",
        "NAME": "test_postgres",
        "USER": "postgres",
        "PASSWORD": "postgres",
    },
}

ALLOWED_HOSTS = ["localhost"]

CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# Disable throttling in tests. With APITestCase (which wraps tests in
# transactions), the in-memory throttle cache persists across tests and
# can cause spurious 429 responses. Throttling is not relevant for
# functional test correctness.
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # noqa: F405

# Disable cost policy debouncing in tests so evaluations happen immediately.
# Tests that specifically test debounce behavior override this per-test.
WALDUR_COST_POLICY_DEBOUNCE_SECONDS = 0
