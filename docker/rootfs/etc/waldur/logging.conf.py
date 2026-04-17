# Logging with structlog
# See also: https://docs.djangoproject.com/en/4.2/ref/settings/#logging
import sys

import structlog

# Processors for stdlib loggers (foreign_pre_chain)
_FOREIGN_PRE_CHAIN = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.ExtraAdder(),
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,  # fixes Celery beat logging
    "formatters": {
        "structlog_json": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.processors.JSONRenderer(),
            "foreign_pre_chain": _FOREIGN_PRE_CHAIN,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structlog_json",
            "level": "DEBUG",
            "stream": sys.stdout,
        },
        "database": {
            "class": "waldur_core.logging.log.DatabaseLogHandler",
            "level": "INFO",
            "formatter": "structlog_json",
        },
    },
    "root": {
        "level": "DEBUG",
        "handlers": ["console", "database"],
    },
    "loggers": {
        "django": {
            "level": "INFO",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "django.server": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        "axes": {
            "level": "WARNING",
            "handlers": ["console"],
            "propagate": False,
        },
        "celery": {
            "level": "INFO",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.app": {
            "level": "INFO",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.app.autodiscover": {
            "level": "WARNING",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.app.base": {
            "level": "WARNING",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.utils": {
            "level": "WARNING",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.utils.imports": {
            "level": "WARNING",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.utils.functional": {
            "level": "WARNING",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.loaders": {
            "level": "WARNING",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.worker": {
            "level": "INFO",
            "handlers": ["console", "database"],
            "propagate": False,
        },
        "celery.bootsteps": {
            "level": "WARNING",
            "handlers": ["console", "database"],
            "propagate": False,
        },
    },
}
