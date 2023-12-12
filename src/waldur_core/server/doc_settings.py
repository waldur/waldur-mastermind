# Django test settings for Waldur Core.
from waldur_core.server.base_settings import *  # noqa

SECRET_KEY = 'test-key'

DEBUG = True

MEDIA_ROOT = '/tmp/'  # noqa: S108

INSTALLED_APPS += ('django_extensions',)  # noqa: F405

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}

ROOT_URLCONF = 'waldur_core.server.urls'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'waldur',
    }
}

ALLOWED_HOSTS = ['localhost']

CELERY_BROKER_URL = 'sqla+sqlite:///:memory:'
CELERY_RESULT_BACKEND = 'db+sqlite:///:memory:'
