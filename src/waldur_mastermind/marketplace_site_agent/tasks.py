import datetime
import logging

from celery import shared_task
from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Max
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.models import PersonalAccessToken
from waldur_core.logging import backend as logging_backend
from waldur_core.logging import enums as logging_enums
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.structure.managers import get_active_tokens
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

    # Also count offerings covered by a UNIFIED consumer bound directly to them
    # (how register_queue binds a migrated site agent). Without this, an operator
    # following the migration runbook — which DELETEs the legacy EventSubscription
    # after cut-over — would silently drop the migrated offering from these hourly
    # safety-net tasks (stale-resource resync, pending-order reminders).
    offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
    site_agent_offering_ids = set(
        marketplace_models.Offering.objects.filter(
            type=SITE_AGENT_OFFERING
        ).values_list("id", flat=True)
    )
    consumer_scopes = (
        logging_models.EventConsumerScope.objects.filter(
            content_type=offering_ct,
            consumer__queue_created=True,
            consumer__user__is_active=True,
        )
        .exclude(consumer__rmq_username="")
        .select_related("consumer")
    )
    for scope in consumer_scopes:
        if scope.object_id not in site_agent_offering_ids:
            continue
        consumer_types = scope.consumer.object_types
        # Empty object_types means "all types".
        if not consumer_types or observable_object_type in consumer_types:
            offering_ids.add(scope.object_id)

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
    threshold = timezone.now() - datetime.timedelta(minutes=10)

    # Aggregate the latest processor run per service in a single query instead of
    # querying the processors of every active service separately. Max() ignores
    # NULL last_run values, and the __lte filter drops services whose processors
    # have never run as well as services without any processors at all.
    stale_agent_services = list(
        models.AgentService.objects.filter(state=enums.AgentServiceState.ACTIVE)
        .annotate(last_processor_run=Max("agentprocessor__last_run"))
        .filter(last_processor_run__lte=threshold)
    )

    if not stale_agent_services:
        return

    models.AgentService.objects.filter(
        id__in=[agent_service.id for agent_service in stale_agent_services]
    ).update(state=enums.AgentServiceState.IDLE, modified=timezone.now())

    for agent_service in stale_agent_services:
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


@shared_task(name="waldur_mastermind.marketplace_site_agent.cleanup_stale_agent_queues")
def cleanup_stale_agent_queues() -> None:
    """Tear down unified-queue RMQ state for consumers whose owner is inactive.

    Mirrors `waldur_core.logging.delete_stale_event_subscriptions` for the
    EventConsumer-backed path: when the owner's credential is no longer active
    (or the user is gone), drop the RMQ queue and user and flip the consumer
    back to an unregistered state.

    An owner counts as active if they hold a live DRF token OR a live Personal
    Access Token. The PAT arm is essential: a PAT-backed agent typically
    has no DRF token at all, so keying off DRF tokens alone would reap every
    PAT-backed agent on the next run.
    """
    active_user_ids = set(get_active_tokens().values_list("user_id", flat=True))
    active_user_ids |= set(
        PersonalAccessToken.objects.filter(
            is_active=True, expires_at__gt=timezone.now()
        ).values_list("user_id", flat=True)
    )

    stale = (
        logging_models.EventConsumer.objects.filter(queue_created=True)
        .exclude(rmq_username="")
        .exclude(user_id__in=active_user_ids)
        .select_related("user")
    )

    rmq_backend = logging_backend.RabbitMQManagementBackend()
    # Client-side chunks: RabbitMQ calls and a save happen between fetches,
    # which a server-side cursor would not survive behind a pooler.
    for consumer in core_utils.chunked_queryset(stale):
        queue_name = consumer.queue_name
        try:
            rmq_backend.delete_queue(consumer.user.uuid.hex, queue_name)
        except Exception as e:
            logger.warning(
                "Failed to delete stale RabbitMQ queue %s: %s", queue_name, e
            )

        try:
            rmq_backend.delete_rabbitmq_user(consumer.rmq_username)
        except Exception as e:
            logger.warning(
                "Failed to delete stale RabbitMQ user %s for consumer %s: %s",
                consumer.rmq_username,
                consumer,
                e,
            )

        consumer.rmq_username = ""
        consumer.queue_created = False
        consumer.save(update_fields=["rmq_username", "queue_created"])
        logger.info("Cleared stale consumer queue state for %s", consumer)


@shared_task(
    name="waldur_mastermind.marketplace_site_agent.cleanup_dangling_agent_queues"
)
def cleanup_dangling_agent_queues() -> None:
    """Sync queue_created with RMQ reality when the RMQ user has vanished.

    Mirrors `waldur_core.logging.delete_dangling_event_subscriptions`: if
    the RMQ user backing an EventConsumer has been removed out of band, the
    queue is already gone — flip `queue_created` so the pre_delete handler
    and re-registrations behave correctly.
    """
    rmq_backend = logging_backend.RabbitMQManagementBackend()

    candidates = logging_models.EventConsumer.objects.filter(
        queue_created=True
    ).exclude(rmq_username="")
    # See cleanup_stale_agent_queues: same pooler-safe walk.
    for consumer in core_utils.chunked_queryset(candidates):
        try:
            rmq_user_info = rmq_backend.get_user(consumer.rmq_username)
        except Exception as exc:
            logger.exception(
                "Unable to query RabbitMQ user for consumer %s: %s", consumer, exc
            )
            continue
        if rmq_user_info is None:
            logger.info(
                "Clearing dangling consumer queue state for %s (RMQ user %s missing)",
                consumer,
                consumer.rmq_username,
            )
            consumer.rmq_username = ""
            consumer.queue_created = False
            consumer.save(update_fields=["rmq_username", "queue_created"])
