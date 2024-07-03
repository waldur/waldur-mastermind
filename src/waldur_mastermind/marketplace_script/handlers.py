import logging

from . import PLUGIN_NAME, tasks

logger = logging.getLogger(__name__)


def resource_options_have_been_changed(sender, instance, created=False, **kwargs):
    if created:
        return

    resource = instance

    if not resource.tracker.has_changed("options"):
        return

    if resource.offering.type != PLUGIN_NAME:
        return

    options_old = resource.tracker.previous("options")
    tasks.resource_options_have_been_changed.delay(resource.id, options_old)
