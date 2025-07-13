import logging

from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.permissions import models as permission_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.models import OfferingUser, Order
from waldur_mastermind.marketplace_site_agent import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)


def send_done_order_to_message_queue(sender, instance: Order, created=False, **kwargs):
    """Send completed marketplace order to message queue for site agent processing."""
    order = instance
    if created:
        return
    offering = order.offering
    if offering.type != PLUGIN_NAME:
        return

    if not order.tracker.has_changed("state") or order.state != OrderStates.DONE:
        return

    payload = {"order_uuid": order.uuid.hex}
    messages = marketplace_utils.prepare_messages(
        offering, payload, logging_utils.ObservableObjectType.ORDER
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def send_pending_order_to_message_queue(
    sender, instance: Order, created=False, **kwargs
):
    """Send pending marketplace order to message queue for site agent processing."""
    order = instance
    if created:
        return

    offering = order.offering
    if offering.type != PLUGIN_NAME:
        return

    if (
        not order.tracker.has_changed("state")
        or order.state != OrderStates.PENDING_PROVIDER
    ):
        return

    payload = {"order_uuid": order.uuid.hex}
    messages = marketplace_utils.prepare_messages(
        offering, payload, logging_utils.ObservableObjectType.ORDER
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def send_offering_user_username_message(
    sender, instance: OfferingUser, created=False, **kwargs
):
    if not created:
        return
    offering_user = instance
    offering = offering_user.offering
    if offering.type != PLUGIN_NAME:
        return

    if not offering_user.tracker.has_changed("username"):
        return

    if not offering_user.username:
        return

    payload = {
        "username": offering_user.username,
        "offering_user_uuid": offering_user.uuid.hex,
        "user_uuid": offering_user.user.uuid.hex,
    }
    messages = marketplace_utils.prepare_messages(
        offering_user.offering,
        payload,
        logging_utils.ObservableObjectType.OFFERING_USER,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def process_role_changed(permission: permission_models.UserRole, granted: bool):
    if not isinstance(permission.scope, structure_models.Project):
        return

    project = permission.scope
    offering_ids = set(
        project.resource_set.filter(
            state=ResourceStates.OK,
            offering__type=PLUGIN_NAME,
        ).values_list("offering", flat=True)
    )

    if not offering_ids:
        return

    user = permission.user
    offerings = marketplace_models.Offering.objects.filter(id__in=offering_ids)

    all_messages = []
    for offering in offerings:
        logger.debug(
            "Processing user role changed event for project %s, offering %s, user %s, granted: %s",
            project,
            offering,
            user,
            granted,
        )
        payload = {
            "user_uuid": user.uuid.hex,
            "user_username": user.username,
            "project_uuid": project.uuid.hex,
            "project_name": project.name,
            "role_name": permission.role.name,
            "granted": granted,
        }
        messages = marketplace_utils.prepare_messages(
            offering, payload, logging_utils.ObservableObjectType.USER_ROLE
        )
        all_messages.extend(messages)

    if all_messages:
        logging_tasks.publish_messages.delay(all_messages)


def send_role_revoked_message_to_mqtt(
    sender, instance: permission_models.UserRole, **kwargs
):
    process_role_changed(instance, False)


def send_role_granted_message_to_mqtt(
    sender, instance: permission_models.UserRole, **kwargs
):
    process_role_changed(instance, True)


def send_resource_update_message_to_mqtt(
    sender, instance: marketplace_models.Resource, created=False, **kwargs
):
    if created:
        return

    offering = instance.offering
    if offering.type != PLUGIN_NAME:
        return

    if not any(
        instance.tracker.has_changed(field_name)
        for field_name in ["downscaled", "restrict_member_access", "paused", "limits"]
    ):
        return

    utils.push_resource_update_message(instance)
