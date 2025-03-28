import logging

from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.permissions import models as permission_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)


def send_pending_order_to_message_queue(sender, instance, created=False, **kwargs):
    order: marketplace_models.Order = instance
    if created:
        return

    offering = order.offering
    if offering.type != PLUGIN_NAME:
        return

    if (
        not order.tracker.has_changed("state")
        or order.state != marketplace_models.Order.States.PENDING_PROVIDER
    ):
        return

    payload = {"order_uuid": order.uuid.hex}
    messages = utils.prepare_messages(
        offering, payload, logging_utils.ObservableObjectType.ORDER
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def process_role_changed(permission: permission_models.UserRole, granted: bool):
    if not isinstance(permission.scope, structure_models.Project):
        return

    project = permission.scope
    offering_ids = set(
        project.resource_set.filter(
            state=marketplace_models.Resource.States.OK,
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
        messages = utils.prepare_messages(
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
