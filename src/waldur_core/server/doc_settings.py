# Django settings for generating documentation for Waldur Core.
from waldur_core.server.base_settings import *  # noqa

SECRET_KEY = 'test-key'

DEBUG = True

ALLOWED_HOSTS = ['127.0.0.1']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}

CELERY_BROKER_URL = 'sqla+sqlite:///:memory:'
CELERY_RESULT_BACKEND = 'db+sqlite:///:memory:'
