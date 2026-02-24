import logging
import logging.config
import os

import structlog
from celery import Celery, signals
from celery.signals import setup_logging
from django_structlog.celery.steps import DjangoStructLogInitStep

from waldur_core.logging.middleware import (
    get_event_context,
    reset_event_context,
    set_event_context,
)

# set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "waldur_core.server.settings")  # XXX:

app = Celery("waldur_core", namespace="CELERY", strict_typing=False)

# Initialize structlog in Celery workers
app.steps["worker"].add(DjangoStructLogInitStep)

# Using a string here means the worker will not have to
# pickle the object when using Windows.
app.config_from_object("django.conf:settings")
app.autodiscover_tasks()


class PriorityRouter:
    """Run heavy tasks and background tasks in separate queues."""

    def route_for_task(self, task_name, *args, **kwargs):
        task = app.tasks.get(task_name)
        if getattr(task, "is_heavy_task", False):
            return {"queue": "heavy-durable"}
        if getattr(task, "is_background", False):
            return {"queue": "background-durable"}
        return None


# The workflow for passing event context to background tasks works as following:
# 1) Generate event context at CaptureEventContextMiddleware and bind it to local thread
# 2) At the Django side: fetch event context from local thread and pass it as parameter to task
# 3) At Celery worker side: fetch event context from task and bind it to local thread
@signals.before_task_publish.connect
def pass_event_context(sender=None, body=None, **kwargs):
    if body is None:
        return

    event_context = get_event_context()
    if event_context:
        # kwargs is the second item in body tuple with index equal 1.
        # See also http://docs.celeryproject.org/en/v4.1.0/internals/protocol.html#version-2
        body[1]["event_context"] = event_context


@signals.task_prerun.connect
def bind_event_context(sender=None, **kwargs):
    try:
        event_context = kwargs["kwargs"].pop("event_context")
    except KeyError:
        return

    set_event_context(event_context)


@signals.task_postrun.connect
def unbind_event_context(sender=None, **kwargs):
    reset_event_context()


@setup_logging.connect
def _configure_structlog_for_celery(loglevel, logfile, format, colorize, **kwargs):
    """Configure structlog when Celery sets up logging for workers."""
    from django.conf import settings

    if not getattr(settings, "DJANGO_STRUCTLOG_CELERY_ENABLED", False):
        return

    from waldur_core.server.base_settings import _FOREIGN_PRE_CHAIN

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
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
                },
            },
            "root": {
                "level": logging.getLevelName(loglevel) if loglevel else "INFO",
                "handlers": ["console"],
            },
        }
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
