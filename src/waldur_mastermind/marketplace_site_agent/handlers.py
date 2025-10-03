import logging

from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.permissions import models as permission_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import OfferingUser, Order
from waldur_mastermind.marketplace_site_agent import utils

logger = logging.getLogger(__name__)


def send_done_order_to_message_queue(sender, instance: Order, created=False, **kwargs):
    """Send completed marketplace order to message queue for site agent processing."""
    order = instance
    if created:
        return
    offering = order.offering
    if offering.type != SITE_AGENT_OFFERING:
        return

    if not order.tracker.has_changed("state") or order.state != OrderStates.DONE:
        return

    payload = {"order_uuid": order.uuid.hex, "order_state": order.get_state_display()}
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

    offering = order.offering
    if offering.type != SITE_AGENT_OFFERING:
        return

    if not order.tracker.has_changed("state") or order.state not in [
        OrderStates.PENDING_PROVIDER,
        OrderStates.PENDING_CONSUMER,
    ]:
        return

    payload = {"order_uuid": order.uuid.hex, "order_state": order.get_state_display()}
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
    if offering.type != SITE_AGENT_OFFERING:
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
            offering__type=SITE_AGENT_OFFERING,
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


def send_role_revoked_message_to_queue(
    sender, instance: permission_models.UserRole, **kwargs
):
    process_role_changed(instance, False)


def send_role_granted_message_to_queue(
    sender, instance: permission_models.UserRole, **kwargs
):
    process_role_changed(instance, True)


def send_resource_update_message_to_queue(
    sender, instance: marketplace_models.Resource, created=False, **kwargs
):
    if created:
        return

    offering = instance.offering
    if offering.type != SITE_AGENT_OFFERING:
        return

    if not any(
        instance.tracker.has_changed(field_name)
        for field_name in ["downscaled", "restrict_member_access", "paused"]
    ):
        return

    utils.push_resource_update_message(instance)


def send_account_message(
    account: marketplace_models.ProjectServiceAccount
    | marketplace_models.CourseAccount,
    created=True,
):
    action = "create" if created else "delete"
    project = account.project
    username = ""
    observable_object_type = logging_utils.ObservableObjectType.SERVICE_ACCOUNT
    match account:
        case service_account if isinstance(
            account, marketplace_models.ProjectServiceAccount
        ):
            username = service_account.username
            observable_object_type = logging_utils.ObservableObjectType.SERVICE_ACCOUNT
        case course_account if isinstance(account, marketplace_models.CourseAccount):
            username = course_account.user.username
            observable_object_type = logging_utils.ObservableObjectType.COURSE_ACCOUNT
    payload = {
        "account_uuid": account.uuid.hex,
        "account_username": username,
        "scope_type": "project",
        "project_uuid": project.uuid.hex,
        "project_name": project.name,
        "action": action,
    }

    logger.info("Sending %s message for the %s", action, account)

    offering_ids = set(
        project.resource_set.filter(
            offering__type=SITE_AGENT_OFFERING,
        )
        .exclude(state=ResourceStates.TERMINATED)
        .values_list("offering", flat=True)
    )
    offerings = marketplace_models.Offering.objects.filter(id__in=offering_ids)
    all_messages = []
    for offering in offerings:
        logger.debug(
            "Processing (%s) account event for project %s, offering %s, username %s",
            action,
            project,
            offering,
            username,
        )
        messages = marketplace_utils.prepare_messages(
            offering, payload, observable_object_type
        )
        all_messages.extend(messages)

    if all_messages:
        logging_tasks.publish_messages.delay(all_messages)


def send_project_service_account_info(
    sender, instance: marketplace_models.ProjectServiceAccount, **kwargs
):
    if not instance.tracker.has_changed("username") or not instance.username:
        return

    send_account_message(instance, created=True)


def send_project_service_account_deletion_info(
    sender, instance: marketplace_models.ProjectServiceAccount, **kwargs
):
    if (
        not instance.tracker.has_changed("state")
        or instance.state != marketplace_enums.ServiceAccountState.CLOSED
    ):
        return

    send_account_message(instance, created=False)


def send_course_account_info(
    sender, instance: marketplace_models.CourseAccount, **kwargs
):
    if not instance.tracker.has_changed("user") or not instance.user:
        return

    send_account_message(instance, created=True)


def send_course_account_deletion_info(
    sender, instance: marketplace_models.CourseAccount, **kwargs
):
    if (
        not instance.tracker.has_changed("state")
        or instance.state != marketplace_enums.CourseAccountState.CLOSED
    ):
        return

    send_account_message(instance, created=False)
