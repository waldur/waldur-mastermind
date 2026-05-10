import datetime
import logging

from celery import shared_task
from constance import config
from django.db.models import Count
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.logging import enums as logging_enums
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OfferingStates,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace_site_agent import enums, models, utils

logger = logging.getLogger(__name__)


def get_offering_ids_for_active_subscriptions(observable_object_type: str):
    """Get all offering IDs linked to active subscriptions with the specified type."""
    # Get active subscriptions and their users in a single query
    active_subscriptions = logging_models.EventSubscription.objects.filter(
        user__is_active=True,
        observable_objects__contains=[{"object_type": observable_object_type}],
    ).select_related("user")

    # Get all available offerings by combining results from each user
    offering_ids = set()
    for subscription in active_subscriptions:
        user_offerings = (
            marketplace_models.Offering.objects.all()
            .filter_for_user(subscription.user)
            .filter(type=SITE_AGENT_OFFERING)
            .values_list("id", flat=True)
        )
        offering_ids.update(user_offerings)

    return offering_ids


@shared_task(name="waldur_mastermind.marketplace_site_agent.sync_offering_users")
def sync_offering_users():
    offerings = marketplace_models.Offering.objects.filter(
        type=SITE_AGENT_OFFERING,
        state__in=[
            OfferingStates.ACTIVE,
            OfferingStates.PAUSED,
        ],
        plugin_options__service_provider_can_create_offering_user=True,
    ).exclude(
        plugin_options__username_generation_policy=marketplace_utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
    )

    marketplace_utils.user_offerings_mapping(offerings)


@shared_task(
    name="waldur_mastermind.marketplace_site_agent.mark_offering_backend_as_disconnected_after_timeout"
)
def mark_offering_backend_as_disconnected_after_timeout():
    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    integration_statuses = marketplace_models.IntegrationStatus.objects.filter(
        status=marketplace_models.IntegrationStatus.States.ACTIVE,
        offering__type=SITE_AGENT_OFFERING,
        last_request_timestamp__lt=one_hour_ago,
    )
    for integration_status in integration_statuses:
        integration_status.set_backend_disconnected()
        integration_status.save(update_fields=["status"])


@shared_task(name="waldur_mastermind.marketplace_site_agent.sync_resources")
def sync_resources():
    """
    Sync resources that haven't been updated in the last hour.
    Processes only resources that users have subscribed to receive updates for.
    """
    offering_ids = get_offering_ids_for_active_subscriptions(
        logging_enums.ObservableObjectType.RESOURCE.value
    )

    # Get resources that need updating
    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    resources = marketplace_models.Resource.objects.filter(
        offering__id__in=offering_ids,
        last_sync__lte=one_hour_ago,
        state__in=[ResourceStates.OK, ResourceStates.ERRED],
    ).order_by("last_sync")[:50]

    # Push updates in bulk
    for resource in resources:
        utils.push_resource_update_message(resource)


@shared_task(name="waldur_mastermind.marketplace_site_agent.sync_resource")
def sync_resource(serialized_instance):
    """
    Send a message to Waldur Site Agent for the resource sync.
    Processes only resources that users have subscribed to receive updates for.
    """
    logger.info("Syncing resource %s", serialized_instance)
    resource = core_utils.deserialize_instance(serialized_instance)
    logger.info("Resource %s deserialized", resource)
    # Push update message to Waldur Site Agent (force=True: user-triggered via pull API)
    utils.push_resource_update_message(resource, force=True)


@shared_task(
    name="waldur_mastermind.marketplace_site_agent.send_messages_about_pending_orders"
)
def send_messages_about_pending_orders():
    """Send a message about pending orders created 1 hour ago.

    Uses MessageStateTracker to skip sending if order state hasn't changed
    since the last notification, preventing redundant messages from hourly
    task execution.
    """
    offering_ids = get_offering_ids_for_active_subscriptions(
        logging_enums.ObservableObjectType.ORDER.value
    )

    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    pending_orders = marketplace_models.Order.objects.filter(
        state=OrderStates.PENDING_PROVIDER,
        offering__id__in=offering_ids,
        created__lt=one_hour_ago,
    )

    for order in pending_orders:
        payload = {
            "order_uuid": order.uuid.hex,
            "order_state": order.get_state_display(),
        }

        # Check if content has changed since last send (idempotency)
        if not logging_utils.MessageStateTracker.should_send_message(
            order.uuid.hex,
            logging_enums.ObservableObjectType.ORDER.value,
            payload,
        ):
            logger.debug(
                "Skipping pending order message for %s (content unchanged)", order
            )
            continue

        # Add sequence number for consumer-side ordering
        payload["sequence_number"] = logging_utils.get_next_sequence_number(
            order.uuid.hex, logging_enums.ObservableObjectType.ORDER.value
        )

        offering = order.offering
        messages = marketplace_utils.prepare_messages(
            offering, payload, logging_enums.ObservableObjectType.ORDER
        )
        if messages:
            logging_tasks.publish_messages.delay(messages)
            logger.info("Sent pending order message for %s", order)


@shared_task(
    name="waldur_mastermind.marketplace_site_agent.mark_agent_services_as_inactive"
)
def mark_agent_services_as_inactive():
    active_agent_services = models.AgentService.objects.filter(
        state=enums.AgentServiceState.ACTIVE,
    )

    for agent_service in active_agent_services:
        processor_last = agent_service.agentprocessor_set.order_by("last_run").last()
        if not processor_last:
            continue
        if processor_last.last_run <= timezone.now() - datetime.timedelta(minutes=10):
            agent_service.state = enums.AgentServiceState.IDLE
            agent_service.save(update_fields=["state"])
            logger.info(
                "Agent service %s has been marked as inactive because its processors have not ran for more than 10 minutes.",
                agent_service.name,
            )


@shared_task(name="waldur_mastermind.marketplace_site_agent.cleanup_site_agent_logs")
def cleanup_site_agent_logs():
    """
    Enforce row count limit per agent identity.

    Keeps newest logs, deletes oldest when count exceeds the configured limit.
    Runs periodically to maintain log volume within limits.
    """
    max_rows = config.SITE_AGENT_LOG_MAX_ROWS_PER_IDENTITY

    identities = models.AgentIdentity.objects.annotate(log_count=Count("logs")).filter(
        log_count__gt=max_rows
    )

    for identity in identities:
        total_count = identity.log_count
        cutoff_row = (
            models.SiteAgentLog.objects.filter(agent_identity=identity)
            .order_by("-timestamp")
            .values_list("timestamp", flat=True)[max_rows : max_rows + 1]
        )
        if cutoff_row:
            deleted, _ = models.SiteAgentLog.objects.filter(
                agent_identity=identity, timestamp__lte=cutoff_row[0]
            ).delete()
            logger.info(
                "Cleaned up %d site agent log entries for identity %s (had %d rows, limit %d)",
                deleted,
                identity.uuid,
                total_count,
                max_rows,
            )
