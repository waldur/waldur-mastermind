import logging

from django.contrib.contenttypes.models import ContentType

from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.logging import enums as logging_enums
from waldur_core.logging import tasks as logging_tasks
from waldur_core.permissions import models as permission_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import OfferingUser
from waldur_mastermind.marketplace_site_agent import utils

logger = logging.getLogger(__name__)


def send_offering_user_username_message(
    sender, instance: OfferingUser, created=False, **kwargs
):
    if get_skip_side_effects():
        return
    offering_user = instance
    offering = offering_user.offering
    if offering.type != SITE_AGENT_OFFERING:
        return

    if not offering_user.tracker.has_changed("username"):
        return

    if not offering_user.username:
        return

    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    user_project_ids = permission_models.UserRole.objects.filter(
        user=offering_user.user,
        is_active=True,
        content_type=project_ct,
    ).values_list("object_id", flat=True)

    resource_backend_ids = list(
        marketplace_models.Resource.objects.filter(
            offering=offering_user.offering,
            project_id__in=user_project_ids,
        )
        .exclude(state=ResourceStates.TERMINATED)
        .exclude(backend_id="")
        .values_list("backend_id", flat=True)
    )

    payload = {
        "username": offering_user.username,
        "offering_user_uuid": offering_user.uuid.hex,
        "user_uuid": offering_user.user.uuid.hex,
        "state": offering_user.state,
        "action": "username_set",
        "resource_backend_ids": resource_backend_ids,
    }
    messages = marketplace_utils.prepare_messages(
        offering_user.offering,
        payload,
        logging_enums.ObservableObjectType.OFFERING_USER,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def process_role_changed(permission: permission_models.UserRole, granted: bool):
    if get_skip_side_effects():
        return
    if not isinstance(permission.scope, structure_models.Project):
        return

    project = permission.scope
    offering_ids = set(
        project.resource_set.filter(
            offering__type=SITE_AGENT_OFFERING,
        )
        .exclude(state=ResourceStates.TERMINATED)
        .values_list("offering", flat=True)
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
            offering, payload, logging_enums.ObservableObjectType.USER_ROLE
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
    if get_skip_side_effects():
        return
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

    utils.push_resource_update_message(instance, force=True)


def send_account_message(
    account: marketplace_models.ProjectServiceAccount
    | marketplace_models.CourseAccount,
    created=True,
):
    if get_skip_side_effects():
        return
    action = "create" if created else "delete"
    project = account.project
    username = ""
    observable_object_type = logging_enums.ObservableObjectType.SERVICE_ACCOUNT
    match account:
        case service_account if isinstance(
            account, marketplace_models.ProjectServiceAccount
        ):
            username = service_account.username
            observable_object_type = logging_enums.ObservableObjectType.SERVICE_ACCOUNT
        case course_account if isinstance(account, marketplace_models.CourseAccount):
            username = course_account.user.username if course_account.user else ""
            observable_object_type = logging_enums.ObservableObjectType.COURSE_ACCOUNT
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


def cleanup_agent_identity_queue(sender, instance, **kwargs):
    """Delete the linked EventConsumer when an AgentIdentity is deleted.

    Deleting the row (rather than only tearing down RabbitMQ) matters: the FK is
    SET_NULL, so a surviving consumer keeps `queue_created=True` and an intact
    offering binding, and the dispatcher goes on publishing every event for that
    offering into a queue that no longer exists — until the hourly
    `cleanup_dangling_agent_queues` sweep notices. Re-registering meanwhile
    would stack a second consumer alongside the dangling one.

    The RabbitMQ teardown itself is done by `cleanup_event_consumer_queue`, the
    pre_delete handler on EventConsumer, so there is exactly one implementation
    of it.
    """
    consumer = instance.event_consumer
    if consumer is None:
        return
    # Clear the in-memory reference so the about-to-be-deleted AgentIdentity
    # object doesn't hold a stale pointer to a deleted consumer. (consumer.delete()
    # still issues its own SET_NULL UPDATE against the DB row, which is harmless
    # here since the row is deleted moments later.)
    instance.event_consumer = None
    consumer.delete()


def send_resource_messages_on_project_move(
    sender, project, old_customer, new_customer, **kwargs
):
    """Push a RESOURCE message for every active site-agent resource in a moved project.

    Waldur does not emit a RESOURCE event when a project changes its customer,
    so STOMP-mode agents would never learn about the new account hierarchy.
    force=True bypasses the idempotency guard because the resource's own fields
    (downscaled, paused, …) haven't changed, but the hierarchy has.
    """
    if get_skip_side_effects():
        return

    resources = marketplace_models.Resource.objects.filter(
        project=project,
        offering__type=SITE_AGENT_OFFERING,
        state=ResourceStates.OK,
    )

    for resource in resources:
        utils.push_resource_update_message(resource, force=True)
        logger.info(
            "Pushed resource message for %s after project %s moved from %s to %s",
            resource,
            project,
            old_customer,
            new_customer,
        )
