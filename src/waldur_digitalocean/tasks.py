import logging

from celery import shared_task

from waldur_core.core import utils
from waldur_core.core.tasks import Task
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=300, default_retry_delay=5)
def wait_for_action_complete(self, action_id, serialized_droplet):
    droplet = utils.deserialize_instance(serialized_droplet)
    backend = droplet.get_backend()
    action = backend.manager.get_action(action_id)
    if action.status == "completed":
        backend_droplet = backend.get_droplet(droplet.backend_id)
        droplet.ip_address = backend_droplet.ip_address
        droplet.save(update_fields=["ip_address"])
        return True
    else:
        self.retry()


class LogDropletResized(Task):
    def execute(self, droplet, serialized_size, *args, **kwargs):
        size = utils.deserialize_instance(serialized_size)
        logger.info("Successfully resized droplet %s", droplet.uuid.hex)
        event_logger.emit(
            "Droplet {droplet_name} has been resized.",
            event_type=EventType.DROPLET_RESIZE_SUCCEEDED,
            event_context={"droplet": droplet, "size": size},
            scopes=[droplet, droplet.project, droplet.project.customer],
        )
