# Logging
# See also: https://docs.djangoproject.com/en/2.2/ref/settings/#logging
import sys

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,  # fixes Celery beat logging

    # Formatters
    # Formatter describes the exact format of the log entry.
    # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#formatters
    'formatters': {
        'simple': {
            'format': '%(asctime)s %(levelname)s %(message)s',
        },
    },

    # Handlers
    # Handler determines what happens to each message in a logger.
    # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#handlers
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
            'level': 'DEBUG',
            'stream': sys.stdout,
        },
    },

    # Loggers
    # A logger is the entry point into the logging system.
    # Each logger is a named bucket to which messages can be written for processing.
    # See also: https://docs.djangoproject.com/en/2.2/topics/logging/#loggers
    #
    # Default logger configuration
    'root': {
        'level': 'DEBUG',
        'handlers': ['console'],
    },
}
