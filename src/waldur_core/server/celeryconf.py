import logging
import logging.config
import os

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
    """Apply Django's LOGGING in Celery workers and beat.

    Celery configures logging itself and would otherwise replace the root
    handlers that django.setup() installed. Connecting a receiver here
    pre-empts that — Celery skips its own setup when setup_logging has
    receivers — so re-applying settings.LOGGING makes a worker log exactly
    like the API: same levels, same handlers, DatabaseLogHandler included.
    Without it the handler is dropped and SystemLog rows with source
    "worker" or "beat" can never be written, leaving those filters in the
    admin log viewer permanently empty.

    Only the root level is overridden, so a worker started with -l debug
    still gets one. structlog itself is already configured by base_settings
    at import, so it is not repeated here.
    """
    from django.conf import settings

    if not getattr(settings, "DJANGO_STRUCTLOG_CELERY_ENABLED", False):
        return

    config = settings.LOGGING
    if loglevel is not None:
        level = (
            loglevel if isinstance(loglevel, str) else logging.getLevelName(loglevel)
        )
        # Shallow copies: settings.LOGGING holds live structlog processor
        # instances, and must not be mutated for the rest of the process.
        config = {**config, "root": {**config["root"], "level": level}}

    logging.config.dictConfig(config)
