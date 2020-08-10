# Django settings for Waldur
from waldur_core.server.base_settings import *

import os
from configparser import RawConfigParser

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), '..'))
TEMPLATES[0]['DIRS'] = [os.path.join(BASE_DIR, 'waldur_core', 'templates')]
LOCALE_PATHS = (
    os.path.join(BASE_DIR, 'waldur_core', 'locale'),
)

conf_dir = os.environ.get('WALDUR_BASE_CONFIG_DIR', '/etc/waldur')
data_dir = '/usr/share/waldur'
work_dir = '/var/lib/waldur'
templates_dir = os.path.join(conf_dir, 'templates')

config = RawConfigParser()
config.read(os.path.join(conf_dir, 'core.ini'))

# If these sections and/or options are not set, these values are used as defaults
config_defaults = {
    'global': {
        'debug': 'false',
        'default_from_email': '',
        'media_root': os.path.join(work_dir, 'media'),
        'owner_can_manage_customer': 'false',
        'secret_key': '',
        'show_all_users': 'false',
        'static_root': os.path.join(data_dir, 'static'),
        'template_debug': 'false',
    },
    'auth': {
        'token_lifetime': 3600,
        'session_lifetime': 3600,
    },
    'events': {
        'hook': 'false',
        'log_file': '',  # empty to disable logging events to file
        'log_level': 'INFO',
        'logserver_host': 'localhost',
        'logserver_port': 5959,
        'syslog': 'false',
    },
    'logging': {
        'admin_email': '',  # empty to disable sending errors to admin by email
        'log_file': '',  # empty to disable logging to file
        'log_level': 'INFO',
        'syslog': 'false',
    },
    'postgresql': {
        'host': '',  # empty to connect via local UNIX socket
        'name': 'waldur',
        'password': 'waldur',
        'port': '5432',
        'user': 'waldur',
    },
    'redis': {
        'host': 'localhost',
        'port': '6379',
        'password': '',
    },
    'sentry': {
        'dsn': '',  # Please ensure that Python Sentry SDK is installed.
    },
}

for section, options in config_defaults.items():
    if not config.has_section(section):
        config.add_section(section)
    for option, value in options.items():
        if not config.has_option(section, option):
            config.set(section, option, value)


if config.get('redis', 'password'):
    redis_url = 'redis://:%s@%s:%s' % (config.get('redis', 'password'),
                                       config.get('redis', 'host'),
                                       config.get('redis', 'port'))
else:
    redis_url = 'redis://%s:%s' % (config.get('redis', 'host'), config.get('redis', 'port'))

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config.get('global', 'secret_key')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config.getboolean('global', 'debug')
for tmpl in TEMPLATES:
    tmpl.setdefault('OPTIONS', {})
    tmpl['OPTIONS']['debug'] = config.getboolean('global', 'debug')

# Allow to overwrite templates
TEMPLATES[0]['DIRS'].insert(0, templates_dir)

# For security reason disable browsable API rendering in production
if not DEBUG:
    REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = ('rest_framework.renderers.JSONRenderer',)

MEDIA_ROOT = config.get('global', 'media_root')

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
        'NAME': config.get('postgresql', 'name'),
        'HOST': config.get('postgresql', 'host'),
        'PORT': config.get('postgresql', 'port'),
        'USER': config.get('postgresql', 'user'),
        'PASSWORD': config.get('postgresql', 'password'),
    },
}

# Logging
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,  # fixes Celery beat logging

    # Filters
    # Filter provides additional control over which log records are passed from logger to handler.
    # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#filters
    'filters': {
        # Filter out only events (user-facing messages)
        'is-event': {
            '()': 'waldur_core.logging.log.RequireEvent',
        },
        # Filter out only non-events (not user-facing messages)
        'is-not-event': {
            '()': 'waldur_core.logging.log.RequireNotEvent',
        },
        # Filter out messages from background tasks
        'is-not-background-task': {
            '()': 'waldur_core.logging.log.RequireNotBackgroundTask',
        },
    },

    # Formatters
    # Formatter describes the exact format of the log entry.
    # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#formatters
    'formatters': {
        'message-only': {
            'format': '%(message)s',
        },
        'simple': {
            'format': '%(asctime)s %(levelname)s %(message)s',
        },
    },

    # Handlers
    # Handler determines what happens to each message in a logger.
    # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#handlers
    'handlers': {
        # Send logs to admins by email
        # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#django.utils.log.AdminEmailHandler
        'email-admins': {
            'filters': ['is-not-background-task'],
            'class': 'django.utils.log.AdminEmailHandler',
            'level': 'ERROR',
        },
        # Write logs to file
        # See also: https://docs.python.org/3/library/logging.handlers.html#watchedfilehandler
        'file': {
            'class': 'logging.handlers.WatchedFileHandler',
            'filename': '/dev/null',
            'filters': ['is-not-event'],
            'formatter': 'simple',
            'level': config.get('logging', 'log_level').upper(),
        },
        'file-event': {
            'class': 'logging.handlers.WatchedFileHandler',
            'filename': '/dev/null',
            'filters': ['is-event'],
            'formatter': 'simple',
            'level': config.get('events', 'log_level').upper(),
        },
        # Forward logs to syslog
        # See also: https://docs.python.org/3/library/logging.handlers.html#sysloghandler
        'syslog': {
            'class': 'logging.handlers.SysLogHandler',
            'filters': ['is-not-event'],
            'formatter': 'message-only',
            'level': config.get('logging', 'log_level').upper(),
        },
        'syslog-event': {
            'class': 'logging.handlers.SysLogHandler',
            'filters': ['is-event'],
            'formatter': 'message-only',
            'level': config.get('events', 'log_level').upper(),
        },
        # Send logs to log server
        # Note that waldur_core.logging.log.TCPEventHandler does not support external formatters
        'tcp': {
            'class': 'waldur_core.logging.log.TCPEventHandler',
            'filters': ['is-not-event'],
            'level': config.get('logging', 'log_level').upper(),
        },
        'tcp-event': {
            'class': 'waldur_core.logging.log.TCPEventHandler',
            'filters': ['is-event'],
            'host': config.get('events', 'logserver_host'),
            'level': config.get('events', 'log_level').upper(),
            'port': config.getint('events', 'logserver_port'),
        },
    },

    # Loggers
    # A logger is the entry point into the logging system.
    # Each logger is a named bucket to which messages can be written for processing.
    # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#loggers
    #
    # Default logger configuration
    'root': {
        'level': 'INFO',
    },
    # Default configuration can be overridden on per-module basis
    'loggers': {
        # Celery loggers
        'celery.worker': {
            'handlers': [],
        },
        'django': {
            'handlers': [],
        },
        'waldur_core': {
            'handlers': ['tcp-event'],
            'level': config.get('logging', 'log_level').upper(),
        },
        'requests': {
            'handlers': [],
            'level': 'WARNING',
        },
    },
}

if config.get('logging', 'admin_email') != '':
    ADMINS += (('Admin', config.get('logging', 'admin_email')),)
    LOGGING['loggers']['celery.worker']['handlers'].append('email-admins')
    LOGGING['loggers']['waldur_core']['handlers'].append('email-admins')

if config.get('logging', 'log_file') != '':
    LOGGING['handlers']['file']['filename'] = config.get('logging', 'log_file')
    LOGGING['loggers']['django']['handlers'].append('file')
    LOGGING['loggers']['waldur_core']['handlers'].append('file')

if config.getboolean('logging', 'syslog'):
    LOGGING['handlers']['syslog']['address'] = '/dev/log'
    LOGGING['loggers']['django']['handlers'].append('syslog')
    LOGGING['loggers']['waldur_core']['handlers'].append('syslog')

if config.get('logging', 'log_level').upper() == 'DEBUG':
    # Enabling debugging at http.client level (requests->urllib3->http.client)
    # you will see the REQUEST, including HEADERS and DATA, and RESPONSE with HEADERS but without DATA.
    # the only thing missing will be the response.body which is not logged.
    from http.client import HTTPConnection
    HTTPConnection.debuglevel = 1

    LOGGING['loggers']['requests.packages.urllib3'] = {
        'handlers': ['file'],
        'level': 'DEBUG',
        'propagate': True
    }

if config.get('events', 'log_file') != '':
    LOGGING['handlers']['file-event']['filename'] = config.get('events', 'log_file')
    LOGGING['loggers']['waldur_core']['handlers'].append('file-event')

if config.getboolean('events', 'syslog'):
    LOGGING['handlers']['syslog-event']['address'] = '/dev/log'
    LOGGING['loggers']['waldur_core']['handlers'].append('syslog-event')

# Static files
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#static-files
STATIC_ROOT = config.get('global', 'static_root')

# Django cache
# https://docs.djangoproject.com/en/2.2/topics/cache/
CACHES['default']['LOCATION'] = redis_url

# Email
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#default-from-email
if config.get('global', 'default_from_email') != '':
    DEFAULT_FROM_EMAIL = config.get('global', 'default_from_email')

# Session
# https://docs.djangoproject.com/en/2.2/ref/settings/#sessions
SESSION_COOKIE_AGE = config.getint('auth', 'session_lifetime')

# Celery
# See also:
#  - http://docs.celeryproject.org/en/latest/userguide/configuration.html
#  - http://docs.celeryproject.org/en/latest/userguide/configuration.html#broker-settings
#  - http://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-result_backend
CELERY_BROKER_URL = redis_url
CELERY_RESULT_BACKEND = redis_url

for app in INSTALLED_APPS:
    if app.startswith('waldur_') and not app.startswith('waldur_core'):
        LOGGING['loggers'][app] = LOGGING['loggers']['waldur_core']

# Waldur Core internal configuration
# See also: http://docs.waldur.com/
WALDUR_CORE.update({
    'TOKEN_LIFETIME': timedelta(seconds=config.getint('auth', 'token_lifetime')),
    'OWNER_CAN_MANAGE_CUSTOMER': config.getboolean('global', 'owner_can_manage_customer'),
    'SHOW_ALL_USERS': config.getboolean('global', 'show_all_users'),
})

# Swagger uses DRF session authentication which can be enabled in DEBUG mode
if config.getboolean('global', 'debug'):
    SWAGGER_SETTINGS['USE_SESSION_AUTH'] = True
    SWAGGER_SETTINGS['LOGIN_URL'] = 'rest_framework:login'
    SWAGGER_SETTINGS['LOGOUT_URL'] = 'rest_framework:logout'

# Sentry integration
# See also: https://docs.getsentry.com/hosted/clients/python/integrations/django/
if config.get('sentry', 'dsn') != '':
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=config.get('sentry', 'dsn'),
        integrations=[DjangoIntegration()],
    )

# Additional configuration files for Waldur
# 'override.conf.py' must be the first element to override settings in core.ini but not plugin configuration.
# Plugin configuration files must me ordered alphabetically to provide predictable configuration handling order.
extensions = ('override.conf.py', 'saml2.conf.py')
for extension_name in extensions:
    # optionally load extension configurations
    extension_conf_file_path = os.path.join(conf_dir, extension_name)
    if os.path.isfile(extension_conf_file_path):
        exec(open(extension_conf_file_path, encoding='utf-8').read())  # nosec
