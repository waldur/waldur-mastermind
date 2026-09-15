import logging

from django.utils.translation import gettext_lazy as _

from waldur_core.logging import enums as logging_enums
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING

logger = logging.getLogger(__name__)

#: Agent backend types that store a limit as a whole number, so a fractional one
#: is truncated at the backend boundary. Taken from the enumeration in
#: waldur/waldur-site-agent#20, which owns this knowledge and where the
#: truncation itself is being fixed.
#:
#: Advisory only: it decides what a provider is *told*, never what they are
#: allowed to configure, so being incomplete costs a missing warning and nothing
#: more. A backend absent from this set is not asserted to hold fractions.
WHOLE_NUMBER_BACKEND_TYPES = frozenset(
    {
        "slurm",
        "moab",
        "mup",
        "harbor",
        "nextcloud",
        "digitalocean",
    }
)


def get_limit_precision_advisory(offering) -> str | None:
    """Warn a provider whose agent cannot hold the precision they are setting.

    One offering type fronts every site agent, and its backends disagree about
    whether a limit can carry a fraction -- litellm and the Kubernetes backends
    need one, SLURM and friends cannot express one at all. So the plugin
    declares no ``max_limit_decimal_places``: a cap covering the type would
    refuse configurations that are perfectly valid for the backend actually
    behind the offering.

    What is known per offering is the backend its agent reports. Where that
    backend stores whole numbers, a fractional limit is ordered, priced and
    invoiced at full value and then truncated on the far side with nothing
    logged -- so the provider is told, and left to decide.

    Silent unless an agent has actually reported a backend known to truncate:
    an offering with no agent yet, or one fronting a backend that holds
    fractions, gets nothing.
    """
    from waldur_mastermind.marketplace_site_agent import models

    reported = set(
        models.AgentProcessor.objects.filter(
            service__identity__offering=offering
        ).values_list("backend_type", flat=True)
    )
    truncating = sorted(
        backend_type
        for backend_type in reported
        if backend_type and backend_type.lower() in WHOLE_NUMBER_BACKEND_TYPES
    )
    if not truncating:
        return None
    return _(
        "This offering is served by a site agent reporting backend "
        "%(backends)s, which stores limits as whole numbers. A fractional "
        "limit would be ordered and invoiced at its full value and then "
        "truncated by the backend."
    ) % {"backends": ", ".join(truncating)}


def push_resource_update_message(
    resource: marketplace_models.Resource, force: bool = False
) -> None:
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
    User-triggered callers should pass force=True to always send.

    Args:
        resource: Resource instance containing the updated information
        force: If True, always send the message regardless of whether
            content has changed. The cache is still updated so that
            subsequent periodic checks remain accurate.

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

    # Always call should_send_message to keep cache updated.
    # When force=True (user-triggered), ignore the result and send anyway.
    should_send = logging_utils.MessageStateTracker.should_send_message(
        resource.uuid.hex,
        logging_enums.ObservableObjectType.RESOURCE.value,
        payload,
    )
    if not force and not should_send:
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


def push_resource_user_role_sync_message(
    resource: marketplace_models.Resource,
) -> None:
    """Send a user role sync message scoped to a single resource.

    Same channel as the project-level trigger (USER_ROLE observable),
    with resource_uuid added to the payload so agents that understand
    it re-sync just this resource; older agents fall back to their
    project-wide handling of the same message.
    """
    # Consistent with push_user_role_sync_message: pubsub messages are
    # only published for site-agent offerings.
    if resource.offering.type != SITE_AGENT_OFFERING:
        logger.debug(
            "Resource %s offering is not a site-agent offering; skipping sync message",
            resource,
        )
        return
    logger.info("Sending user role sync message for resource %s", resource)
    payload = {
        "project_uuid": resource.project.uuid.hex,
        "project_name": resource.project.name,
        "resource_uuid": resource.uuid.hex,
    }
    messages = marketplace_utils.prepare_messages(
        resource.offering, payload, logging_enums.ObservableObjectType.USER_ROLE
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)
        logger.info(
            "Sent %d user role sync messages for resource %s", len(messages), resource
        )
    else:
        logger.debug("No messages to send for resource %s", resource)


def push_user_role_sync_message(project: structure_models.Project) -> None:
    """
    Send user role sync message for a project.

    Args:
        project: Project instance to sync
    """
    logger.info("Sending user role sync message for project %s", project)
    offering_ids = set(
        project.resource_set.filter(
            offering__type=SITE_AGENT_OFFERING,
        )
        .exclude(state=marketplace_models.ResourceStates.TERMINATED)
        .values_list("offering", flat=True)
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


def resolve_offering_agent_authorization(request, offering, agent_identity=None):
    """Which permission branch lets this user manage the offering's agent, if any.

    Returns the :class:`ConsumerAuthorization` member for the branch that
    passed, or None when none does. The branch is recorded on the EventConsumer
    at registration, so an operator can tell a queue authorised by a staff
    account from one authorised by an offering-scoped role.

    Allowed for:
    1. Staff
    2. Customer-level permission (owner, service provider manager)
    3. Offering managers (offering-scoped role)
    4. Identity managers with managed_isds — can create for non-archived/draft
       offerings and manage only their own agent identities
    """
    user = request.user
    if user.is_staff:
        return logging_enums.ConsumerAuthorization.STAFF
    if has_permission(request, PermissionEnum.CREATE_OFFERING, offering.customer):
        return logging_enums.ConsumerAuthorization.CUSTOMER_OWNER
    if has_permission(request, PermissionEnum.UPDATE_OFFERING, offering):
        return logging_enums.ConsumerAuthorization.OFFERING_MANAGER
    if user.is_identity_manager and user.managed_isds:
        if offering.state not in marketplace_enums.OfferingStates.ISD_ALLOWED_STATES:
            return None
        if agent_identity is not None and agent_identity.created_by != user:
            return None
        return logging_enums.ConsumerAuthorization.IDENTITY_MANAGER
    return None


def can_manage_offering_agent(request, offering, agent_identity=None):
    """Check if user can manage agent identities/services for the given offering."""
    return (
        resolve_offering_agent_authorization(request, offering, agent_identity)
        is not None
    )
