# Django settings for Waldur
from waldur_core.server.base_settings import *

import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), '..'))
TEMPLATES[0]['DIRS'] = [os.path.join(BASE_DIR, 'waldur_core', 'templates')]
LOCALE_PATHS = (
    os.path.join(BASE_DIR, 'waldur_core', 'locale'),
)

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

env: dict = os.environ

conf_dir = env.get('WALDUR_BASE_CONFIG_DIR', '/etc/waldur')
data_dir = '/usr/share/waldur'
work_dir = '/var/lib/waldur'
templates_dir = os.path.join(conf_dir, 'templates')

SECRET_KEY = env.get('GLOBAL_SECRET_KEY')

media_root: str = os.path.join(work_dir, 'media')

redis_password: str = env.get('REDIS_PASSWORD')
redis_host: str = env.get('REDIS_HOST', 'localhost')
redis_port: str = env.get('REDIS_PORT', '6379')
if redis_password:
    redis_url = 'redis://:%s@%s:%s' % (redis_password,
                                       redis_host,
                                       redis_port)
else:
    redis_url = 'redis://%s:%s' % (redis_host, redis_port)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env.get('GLOBAL_DEBUG', 'false').lower() == 'true'

for tmpl in TEMPLATES:
    tmpl.setdefault('OPTIONS', {})
    tmpl['OPTIONS']['debug'] = DEBUG

# Allow to overwrite templates
TEMPLATES[0]['DIRS'].insert(0, templates_dir)

# For security reason disable browsable API rendering in production
if not DEBUG:
    REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = ('rest_framework.renderers.JSONRenderer',)

MEDIA_ROOT = media_root

ALLOWED_HOSTS = ['*']

#
# Application definition
#

# Database
#
# Requirements:
#  - PostgreSQL server is running and accessible on 'HOST':'PORT'
#  - PostgreSQL user 'USER' created and can access PostgreSQL server using password 'PASSWORD'
#  - PostgreSQL database 'NAME' created with all privileges granted to user 'USER'
#  - psycopg2 package is installed: https://pypi.python.org/pypi/psycopg2
#
# Note: if PostgreSQL server is running on local host and is accessible via UNIX socket,
# leave 'HOST' and 'PORT' empty. For password usage details in this setup see
# https://www.postgresql.org/docs/9.5/static/auth-methods.html
#
# Example: create database, user and grant privileges:
#
#   CREATE DATABASE waldur ENCODING 'UTF8'
#   CREATE USER waldur WITH PASSWORD 'waldur'
#
# Example: install psycopg2 in CentOS:
#
#   yum install python-psycopg2
#
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#databases
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env.get('POSTGRESQL_NAME', 'waldur'),
        'HOST': env.get('POSTGRESQL_HOST', 'localhost'),
        'PORT': env.get('POSTGRESQL_PORT', '5432'),
        'USER': env.get('POSTGRESQL_USER', 'waldur'),
        'PASSWORD': env.get('POSTGRESQL_PASSWORD', 'waldur'),
    },
}

# Static files
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#static-files
STATIC_ROOT = env.get('GLOBAL_STATIC_ROOT', os.path.join(data_dir, 'static'))

# Django cache
# https://docs.djangoproject.com/en/2.2/topics/cache/
CACHES['default']['LOCATION'] = redis_url

# Email
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#default-from-email
default_from_email = env.get('GLOBAL_DEFAULT_FROM_EMAIL')
if default_from_email:
    DEFAULT_FROM_EMAIL = default_from_email

# Session
# https://docs.djangoproject.com/en/2.2/ref/settings/#sessions
SESSION_COOKIE_AGE = env.get('AUTH_COOKIE_AGE', 3600)

# Celery
# See also:
#  - http://docs.celeryproject.org/en/latest/userguide/configuration.html
#  - http://docs.celeryproject.org/en/latest/userguide/configuration.html#broker-settings
#  - http://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-result_backend
CELERY_BROKER_URL = redis_url
CELERY_RESULT_BACKEND = redis_url

# Waldur Core internal configuration
# See also: http://docs.waldur.com/
token_lifetime = env.get('AUTH_TOKEN_LIFETIME', 3600)
WALDUR_CORE.update({
    'TOKEN_LIFETIME': timedelta(seconds=token_lifetime),
    'OWNER_CAN_MANAGE_CUSTOMER': env.get('GLOBAL_OWNER_CAN_MANAGE_CUSTOMER', 'false').lower() == 'true',
    'SHOW_ALL_USERS': env.get('GLOBAL_SHOW_ALL_USERS',  'false').lower() == 'true',
})

# Swagger uses DRF session authentication which can be enabled in DEBUG mode
if DEBUG:
    SWAGGER_SETTINGS['USE_SESSION_AUTH'] = True
    SWAGGER_SETTINGS['LOGIN_URL'] = 'rest_framework:login'
    SWAGGER_SETTINGS['LOGOUT_URL'] = 'rest_framework:logout'

# Sentry integration
# See also: https://docs.getsentry.com/hosted/clients/python/integrations/django/
sentry_dsn = env.get('SENTRY_DSN')
if sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[DjangoIntegration()],
    )

# Additional configuration files for Waldur
# 'override.conf.py' must be the first element to override settings in core.ini but not plugin configuration.
# Plugin configuration files must me ordered alphabetically to provide predictable configuration handling order.
extensions = ('override.conf.py', 'logging.conf.py', 'saml2.conf.py')
for extension_name in extensions:
    # optionally load extension configurations
    extension_conf_file_path = os.path.join(conf_dir, extension_name)
    if os.path.isfile(extension_conf_file_path):
        exec(open(extension_conf_file_path, encoding='utf-8').read())  # nosec

if not SECRET_KEY:
    raise Exception('GLOBAL_SECRET_KEY is not set')
