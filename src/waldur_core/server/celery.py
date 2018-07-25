from __future__ import absolute_import

import os

from celery import Celery
from celery import signals

from waldur_core.logging.middleware import get_event_context, set_event_context, reset_event_context

# set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'waldur_core.server.settings')  # XXX:

app = Celery('waldur_core', namespace='CELERY', strict_typing=False)

# Using a string here means the worker will not have to
# pickle the object when using Windows.
app.config_from_object('django.conf:settings')
app.autodiscover_tasks()


class PriorityRouter(object):
    """ Run heavy tasks and background tasks in separate queues. """

    def route_for_task(self, task_name, *args, **kwargs):
        task = app.tasks.get(task_name)
        if getattr(task, 'is_heavy_task', False):
            return {'queue': 'heavy'}
        if getattr(task, 'is_background', False):
            return {'queue': 'background'}
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
        body[1]['event_context'] = event_context


@signals.task_prerun.connect
def bind_event_context(sender=None, **kwargs):
    try:
        event_context = kwargs['kwargs'].pop('event_context')
    except KeyError:
        return

    set_event_context(event_context)


@signals.task_postrun.connect
def unbind_event_context(sender=None, **kwargs):
    reset_event_context()
