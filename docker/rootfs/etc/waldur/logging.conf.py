# Logging
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#logging
import os

env: dict = os.environ

logging_log_level: str = env.get('LOGGING_LOG_LEVEL', 'INFO').upper()
events_log_level: str = env.get('EVENTS_LOG_LEVEL', 'INFO').upper()

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
            'level': logging_log_level,
        },
        'file-event': {
            'class': 'logging.handlers.WatchedFileHandler',
            'filename': '/dev/null',
            'filters': ['is-event'],
            'formatter': 'simple',
            'level': events_log_level,
        },
        # Forward logs to syslog
        # See also: https://docs.python.org/3/library/logging.handlers.html#sysloghandler
        'syslog': {
            'class': 'logging.handlers.SysLogHandler',
            'filters': ['is-not-event'],
            'formatter': 'message-only',
            'level': logging_log_level,
        },
        'syslog-event': {
            'class': 'logging.handlers.SysLogHandler',
            'filters': ['is-event'],
            'formatter': 'message-only',
            'level': events_log_level,
        },
        # Send logs to log server
        # Note that waldur_core.logging.log.TCPEventHandler does not support external formatters
        'tcp': {
            'class': 'waldur_core.logging.log.TCPEventHandler',
            'filters': ['is-not-event'],
            'level': logging_log_level,
        },
        'tcp-event': {
            'class': 'waldur_core.logging.log.TCPEventHandler',
            'filters': ['is-event'],
            'host': env.get('EVENTS_LOGSERVER_HOST', 'localhost'),
            'level': events_log_level,
            'port': int(env.get('EVENTS_LOGSERVER_PORT', 5959)),
        },
        'console': {
            'class': 'logging.StreamHandler',
            'filters': ['is-not-event'],
            'formatter': 'simple',
            'level': logging_log_level,
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
            'handlers': ['console'],
        },
        'django': {
            'handlers': ['console'],
        },
        'waldur_core': {
            'handlers': ['tcp-event', 'console'],
            'level': logging_log_level,
        },
        'requests': {
            'handlers': [],
            'level': 'WARNING',
        },
    },
}

logging_admin_email = env.get('LOGGING_ADMIN_EMAIL')
if logging_admin_email:
    ADMINS += (('Admin', logging_admin_email),)
    LOGGING['loggers']['celery.worker']['handlers'].append('email-admins')
    LOGGING['loggers']['waldur_core']['handlers'].append('email-admins')

logging_log_file = env.get('LOGGING_LOG_FILE')

if logging_log_file:
    LOGGING['handlers']['file']['filename'] = logging_log_file
    LOGGING['loggers']['django']['handlers'].append('file')
    LOGGING['loggers']['waldur_core']['handlers'].append('file')

logging_syslog: bool = env.get('LOGGING_SYSLOG', 'false').lower() == 'true'

if logging_syslog:
    LOGGING['handlers']['syslog']['address'] = '/dev/log'
    LOGGING['loggers']['django']['handlers'].append('syslog')
    LOGGING['loggers']['waldur_core']['handlers'].append('syslog')

if logging_log_level == 'DEBUG':
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

events_log_file = env.get('EVENTS_LOG_FILE')

if events_log_file:
    LOGGING['handlers']['file-event']['filename'] = events_log_file
    LOGGING['loggers']['waldur_core']['handlers'].append('file-event')

if logging_syslog:
    LOGGING['handlers']['syslog-event']['address'] = '/dev/log'
    LOGGING['loggers']['waldur_core']['handlers'].append('syslog-event')

for app in INSTALLED_APPS:
    if app.startswith('waldur_') and not app.startswith('waldur_core'):
        LOGGING['loggers'][app] = LOGGING['loggers']['waldur_core']
