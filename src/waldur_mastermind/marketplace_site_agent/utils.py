import logging

from waldur_core.logging import enums as logging_enums
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING

logger = logging.getLogger(__name__)


def push_resource_update_message(resource: marketplace_models.Resource) -> None:
    """
    Push resource update message to queue topic for notification purposes.

    This function prepares and sends a message containing resource state updates
    to event subscribers. The message includes:
    - Resource UUID
    - Resource backend ID
    - State flags (downscaled, restrict_member_access, paused)
    - Sequence number for ordering

    Uses MessageStateTracker to skip sending if content hasn't changed since
    the last send, preventing redundant messages from periodic sync tasks.

    Args:
        resource: Resource instance containing the updated information

    Example payload:
        {
            "resource_uuid": "abc123...",
            "resource_backend_id": "slurm-123",
            "downscaled": false,
            "restrict_member_access": true,
            "paused": false,
            "sequence_number": 42
        }
    """
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
            ]
        }
    )

    # Check if content has changed since last send (idempotency)
    if not logging_utils.MessageStateTracker.should_send_message(
        resource.uuid.hex,
        logging_enums.ObservableObjectType.RESOURCE.value,
        payload,
    ):
        logger.debug(
            "Skipping resource update message for %s (content unchanged)", resource
        )
        return

    # Add sequence number for consumer-side ordering
    payload["sequence_number"] = logging_utils.get_next_sequence_number(
        resource.uuid.hex, logging_enums.ObservableObjectType.RESOURCE.value
    )

    logger.info("Sending resource update message to topic for %s", resource)

    messages = marketplace_utils.prepare_messages(
        resource.offering, payload, logging_enums.ObservableObjectType.RESOURCE
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
            offering__type=SITE_AGENT_OFFERING,
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
        messages = marketplace_utils.prepare_messages(
            offering, payload, logging_enums.ObservableObjectType.USER_ROLE
        )
        all_messages.extend(messages)
    if all_messages:
        logging_tasks.publish_messages.delay(all_messages)
        logger.info(
            "Sent %d user role sync messages for project %s", len(all_messages), project
        )
    else:
        logger.debug("No messages to send for project %s", project)
