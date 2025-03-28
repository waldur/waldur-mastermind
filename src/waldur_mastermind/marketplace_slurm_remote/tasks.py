import datetime

from celery import shared_task
from django.utils import timezone

from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME, utils


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
            .filter(type=PLUGIN_NAME)
            .values_list("id", flat=True)
        )
        offering_ids.update(user_offerings)

    return offering_ids


@shared_task(name="waldur_mastermind.marketplace_slurm_remote.sync_offering_users")
def sync_offering_users():
    offerings = marketplace_models.Offering.objects.filter(
        type=PLUGIN_NAME,
        state__in=[
            marketplace_models.Offering.States.ACTIVE,
            marketplace_models.Offering.States.PAUSED,
        ],
        plugin_options__service_provider_can_create_offering_user=True,
    ).exclude(
        plugin_options__username_generation_policy=marketplace_utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
    )

    marketplace_utils.user_offerings_mapping(offerings)


@shared_task(
    name="waldur_mastermind.marketplace_slurm_remote.mark_offering_backend_as_disconnected_after_timeout"
)
def mark_offering_backend_as_disconnected_after_timeout():
    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    integration_statuses = marketplace_models.IntegrationStatus.objects.filter(
        status=marketplace_models.IntegrationStatus.States.ACTIVE,
        offering__type=PLUGIN_NAME,
        last_request_timestamp__lt=one_hour_ago,
    )
    for integration_status in integration_statuses:
        integration_status.set_backend_disconnected()
        integration_status.save(update_fields=["status"])


@shared_task(name="waldur_mastermind.marketplace_slurm_remote.sync_resources")
def sync_resources():
    """
    Sync resources that haven't been updated in the last hour.
    Only processes resources that users have subscribed to receive updates for.
    """
    offering_ids = get_offering_ids_for_active_subscriptions(
        logging_utils.ObservableObjectType.RESOURCE.value
    )

    # Get resources that need updating
    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    resources = marketplace_models.Resource.objects.filter(
        offering__id__in=offering_ids, last_sync__lte=one_hour_ago
    ).order_by("last_sync")[:50]

    # Push updates in bulk
    for resource in resources:
        utils.push_resource_update_message(resource)


@shared_task(
    name="waldur_mastermind.marketplace_slurm_remote.send_messages_about_pending_orders"
)
def send_messages_about_pending_orders():
    """Send a message about pending orders created 1 hour ago to MQTT"""
    offering_ids = get_offering_ids_for_active_subscriptions(
        logging_utils.ObservableObjectType.ORDER.value
    )

    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    pending_orders = marketplace_models.Order.objects.filter(
        state=marketplace_models.Order.States.PENDING_PROVIDER,
        offering__id__in=offering_ids,
        created__lt=one_hour_ago,
    )

    for order in pending_orders:
        payload = {"order_uuid": order.uuid.hex}
        offering = order.offering
        messages = utils.prepare_messages(
            offering, payload, logging_utils.ObservableObjectType.ORDER
        )
        if messages:
            logging_tasks.publish_messages.delay(messages)
