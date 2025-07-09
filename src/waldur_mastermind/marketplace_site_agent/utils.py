import logging

from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace_site_agent import PLUGIN_NAME

logger = logging.getLogger(__name__)


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

    messages = marketplace_utils.prepare_messages(
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
        messages = marketplace_utils.prepare_messages(
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
