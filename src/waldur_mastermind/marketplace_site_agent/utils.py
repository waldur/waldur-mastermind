import json
import logging

from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_site_agent import PLUGIN_NAME

logger = logging.getLogger(__name__)


def prepare_messages(
    offering: marketplace_models.Offering,
    message_payload: dict,
    affected_object: logging_utils.ObservableObjectType,
) -> list[dict[str, str]]:
    """Helper function to prepare event messages for marketplace events.

    Generates event messages for users who have subscribed to events related to marketplace
    offerings they have access to. Each message includes a vhost, topic and payload.

    Args:
        offering: Marketplace offering instance to generate messages for
        message_payload: Dictionary containing event-specific data to be included in the message
        affected_object: Type of event for the topic name (e.g. "order" or "user_role")

    Returns:
        List of dictionaries, each containing:
            - vhost: User UUID hex string
            - topic: Topic string in format "subscription/{sub_uuid}/offering/{offering_uuid}/{affected_object}"
            - payload: JSON string containing the input payload plus offering_uuid

    Example:
        >>> messages = prepare_messages(
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
        "Preparing messages for event %s, offering %s",
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
        message_payload["offering_uuid"] = offering.uuid.hex
        message_payload_str = json.dumps(message_payload)
        vhost_name = user.uuid.hex
        messages_to_send.append(
            {"vhost": vhost_name, "topic": topic_name, "payload": message_payload_str}
        )

    return messages_to_send


def push_resource_update_message(resource: marketplace_models.Resource) -> None:
    """
    Push resource update message to queue topic for notification purposes.

    This function prepares and sends a message containing resource state updates
    to event subscribers. The message includes:
    - Resource UUID
    - Resource backend ID
    - State flags (downscaled, restrict_member_access, paused)

    Args:
        resource: Resource instance containing the updated information

    Example payload:
        {
            "resource_uuid": "abc123...",
            "resource_backend_id": "slurm-123",
            "downscaled": false,
            "restrict_member_access": true,
            "paused": false
        }
    """
    logger.info("Sending resource update message to topic for %s", resource)

    payload = {
        "resource_uuid": resource.uuid.hex,
        "resource_backend_id": resource.backend_id,
    }
    payload.update(
        {
            field_name: getattr(resource, field_name)
            for field_name in [
                "downscaled",
                "restrict_member_access",
                "paused",
                "limits",
            ]
        }
    )

    messages = prepare_messages(
        resource.offering, payload, logging_utils.ObservableObjectType.RESOURCE
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def push_user_role_sync_message(project: structure_models.Project) -> None:
    """
    Send user role sync message for a project.

    Args:
        project: Project instance to sync
    """
    logger.info("Sending user role sync message for project %s", project)
    offering_ids = set(
        project.resource_set.filter(
            state=marketplace_models.ResourceStates.OK,
            offering__type=PLUGIN_NAME,
        ).values_list("offering", flat=True)
    )
    if not offering_ids:
        logger.debug("No relevant offerings found for project %s", project)
        return
    offerings = marketplace_models.Offering.objects.filter(id__in=offering_ids)
    all_messages = []
    for offering in offerings:
        payload = {
            "project_uuid": project.uuid.hex,
            "project_name": project.name,
        }
        messages = prepare_messages(
            offering, payload, logging_utils.ObservableObjectType.USER_ROLE
        )
        all_messages.extend(messages)
    if all_messages:
        logging_tasks.publish_messages.delay(all_messages)
        logger.info(
            "Sent %d user role sync messages for project %s", len(all_messages), project
        )
    else:
        logger.debug("No messages to send for project %s", project)
