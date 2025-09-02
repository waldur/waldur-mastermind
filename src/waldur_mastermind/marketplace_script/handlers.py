import logging

from waldur_mastermind.marketplace.models import Resource

from ..marketplace.enums import SCRIPT_OFFERING
from . import tasks

logger = logging.getLogger(__name__)


def resource_options_have_been_changed(
    sender, instance: Resource, created=False, **kwargs
):
    """Handle script execution when marketplace resource options are changed."""
    if created:
        return

    resource = instance

    if not resource.tracker.has_changed("options"):
        return

    if resource.offering.type != SCRIPT_OFFERING:
        return

    options_old = resource.tracker.previous("options")
    tasks.resource_options_have_been_changed.delay(resource.id, options_old)
