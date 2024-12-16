import json
import logging

from django.core import exceptions as django_exceptions
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.utils import timezone

from waldur_core.core.utils import month_start
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.permissions import models as permission_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME

logger = logging.getLogger(__name__)

COMPONENT_FIELDS = {
    "cpu_usage",
    "gpu_usage",
    "ram_usage",
    "cpu_limit",
    "gpu_limit",
    "ram_limit",
}


def update_component_quota(sender, instance, created=False, **kwargs):
    if created:
        return

    if not set(instance.tracker.changed()) & COMPONENT_FIELDS:
        return

    allocation = instance

    try:
        resource = marketplace_models.Resource.objects.get(scope=allocation)
    except django_exceptions.ObjectDoesNotExist:
        return

    for component in manager.get_components(PLUGIN_NAME):
        usage = getattr(allocation, component.type + "_usage")
        limit = getattr(allocation, component.type + "_limit")

        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type=component.type
            )
        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning(
                "Skipping Allocation synchronization because this "
                "marketplace.OfferingComponent does not exist."
                "Allocation ID: %s",
                allocation.id,
            )
        else:
            marketplace_models.ComponentQuota.objects.update_or_create(
                resource=resource,
                component=offering_component,
                defaults={"limit": limit, "usage": usage},
            )
            try:
                plan_period = marketplace_models.ResourcePlanPeriod.objects.get(
                    resource=resource, end=None
                )
            except (ObjectDoesNotExist, MultipleObjectsReturned):
                logger.warning(
                    "Skipping component usage synchronization because valid"
                    "ResourcePlanPeriod is not found."
                    "Allocation ID: %s",
                    allocation.id,
                )
            else:
                date = timezone.now()
                marketplace_models.ComponentUsage.objects.update_or_create(
                    resource=resource,
                    component=offering_component,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                    defaults={"usage": usage, "date": date},
                )


def sync_component_user_usage_when_allocation_user_usage_is_submitted(
    sender, instance, **kwargs
):
    marketplace_utils.sync_component_user_usage(instance, PLUGIN_NAME)


def prepare_mqtt_messages(
    offering: marketplace_models.Offering,
    payload: dict,
    affected_object: logging_utils.ObservableObjectType,
) -> list[dict[str, str]]:
    """Helper function to prepare MQTT messages for marketplace events.

    Generates MQTT messages for users who have subscribed to events related to marketplace
    offerings they have access to. Each message includes a vhost, topic and payload.

    Args:
        offering: Marketplace offering instance to generate messages for
        payload: Dictionary containing event-specific data to be included in the message
        affected_object: Type of event for the topic name (e.g. "order" or "user_role")

    Returns:
        List of dictionaries, each containing:
            - vhost: User UUID hex string
            - topic: Topic string in format "subscription/{sub_uuid}/offering/{offering_uuid}/{affected_object}"
            - payload: JSON string containing the input payload plus offering_uuid

    Example:
        >>> messages = prepare_mqtt_messages(
        ...     offering=some_offering,
        ...     payload={"order_uuid": "123"},
        ...     affected_object=ObservableObjectType.ORDER
        ... )
        >>> messages[0]
        {
            'vhost': 'user-uuid-hex',
            'topic': 'subscription/sub-uuid/offering/off-uuid/order',
            'payload': '{"order_uuid": "123", "offering_uuid": "off-uuid"}'
        }
    """

    logger.debug(
        "Preparing MQTT messages for event %s, offering %s",
        affected_object.value,
        offering,
    )
    event_subscriptions = logging_models.EventSubscription.objects.filter(
        observable_objects__contains=[{"object_type": affected_object.value}]
    )

    if not event_subscriptions.exists():
        logger.debug(
            "No event subscriptions exist for %s, skipping message sending",
            affected_object.value,
        )
        return []

    messages_to_send = []
    for event_subscription in event_subscriptions:
        user = event_subscription.user
        logger.info("Processing subscription for user %s", user)

        # Check if user has access to offering
        linked_offerings = marketplace_models.Offering.objects.all().filter_for_user(
            user
        )
        if offering not in linked_offerings:
            logger.debug(
                "The user %s does not have access to the offering %s", user, offering
            )
            continue

        topic_name = f"subscription/{event_subscription.uuid.hex}/offering/{offering.uuid.hex}/{affected_object.value}"
        payload["offering_uuid"] = offering.uuid.hex
        mqtt_payload = json.dumps(payload)
        vhost_name = user.uuid.hex
        messages_to_send.append(
            {"vhost": vhost_name, "topic": topic_name, "payload": mqtt_payload}
        )

    return messages_to_send


def send_order_created_to_mqtt(sender, instance, created=False, **kwargs):
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
    messages = prepare_mqtt_messages(
        offering, payload, logging_utils.ObservableObjectType.ORDER
    )
    if messages:
        logging_tasks.publish_mqtt_messages.delay(messages)


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
        messages = prepare_mqtt_messages(
            offering, payload, logging_utils.ObservableObjectType.USER_ROLE
        )
        all_messages.extend(messages)

    if all_messages:
        logging_tasks.publish_mqtt_messages.delay(all_messages)


def send_role_revoked_message_to_mqtt(
    sender, instance: permission_models.UserRole, **kwargs
):
    process_role_changed(instance, False)


def send_role_granted_message_to_mqtt(
    sender, instance: permission_models.UserRole, **kwargs
):
    process_role_changed(instance, True)
