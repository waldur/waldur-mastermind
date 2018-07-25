import logging
import sys

from celery.task import Task as CeleryTask
from django.utils import six

from waldur_core.core import utils
from waldur_core.core.tasks import BackendMethodTask, Task

from . import backend, log

logger = logging.getLogger(__name__)


class WaitForActionComplete(CeleryTask):
    max_retries = 300
    default_retry_delay = 5

    def run(self, action_id, serialized_droplet):
        droplet = utils.deserialize_instance(serialized_droplet)
        backend = droplet.get_backend()
        action = backend.manager.get_action(action_id)
        if action.status == 'completed':
            backend_droplet = backend.get_droplet(droplet.backend_id)
            droplet.ip_address = backend_droplet.ip_address
            droplet.save(update_fields=['ip_address'])
            return True
        else:
            self.retry()


class LogDropletResized(Task):

    def execute(self, droplet, serialized_size, *args, **kwargs):
        size = utils.deserialize_instance(serialized_size)
        logger.info('Successfully resized droplet %s', droplet.uuid.hex)
        log.event_logger.droplet_resize.info(
            'Droplet {droplet_name} has been resized.',
            event_type='droplet_resize_succeeded',
            event_context={'droplet': droplet, 'size': size}
        )


class SafeBackendMethodTask(BackendMethodTask):
    """
    Open alert if token scope is read-only.
    Close alert if token scope if read-write.
    It should be applied to droplet provisioning tasks.
    """

    def execute(self, droplet, *args, **kwargs):
        try:
            result = super(SafeBackendMethodTask, self).execute(droplet, *args, **kwargs)
        except backend.TokenScopeError:
            droplet.service_project_link.service.raise_readonly_token_alert()
            six.reraise(*sys.exc_info())
        else:
            droplet.service_project_link.service.close_readonly_token_alert()
            return result
