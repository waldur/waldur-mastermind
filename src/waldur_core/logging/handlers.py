import logging

from django.db import transaction
from rest_framework.authtoken.models import Token

from waldur_core.logging import backend, models, tasks, utils
from waldur_core.logging.models import Event, EventSubscriptionQueue, SystemNotification
from waldur_core.logging.tasks import get_matching_hooks

logger = logging.getLogger(__name__)


def process_hook(sender, instance: Event, **kwargs):
    """Process a hook for a given event."""
    if get_matching_hooks(instance) or SystemNotification.objects.filter(
        event_types__contains=instance.event_type
    ):
        transaction.on_commit(lambda: tasks.process_event.delay(instance.pk))


def delete_stale_event_subscriptions(sender, instance: Token, **kwargs):
    """Delete stale event subscriptions for a user when their token is deleted."""
    user = instance.user
    stale_event_subscriptions = models.EventSubscription.objects.filter(user=user)
    if stale_event_subscriptions.count() == 0:
        return

    logger.info(
        "Removing %s stale event subscriptions after user %s token expiration",
        stale_event_subscriptions.count(),
        user,
    )
    utils.delete_stale_subscriptions(stale_event_subscriptions)


def cleanup_rabbitmq_queue_on_delete(
    sender, instance: EventSubscriptionQueue, **kwargs
):
    """Delete the corresponding RabbitMQ queue when an EventSubscriptionQueue record is deleted."""
    from waldur_core.logging.backend import RabbitMQManagementBackend

    rmq_backend = RabbitMQManagementBackend()
    try:
        rmq_backend.delete_queue(instance.vhost, instance.queue_name)
    except Exception as e:
        logger.warning(
            "Failed to delete RabbitMQ queue %s in vhost %s: %s",
            instance.queue_name,
            instance.vhost,
            e,
        )


def cleanup_event_consumer_queue(sender, instance, **kwargs):
    """Tear down the RabbitMQ queue + user when an EventConsumer is deleted.

    Covers standalone (non-agent) consumers, which have no AgentIdentity
    pre_delete handler. A no-op for consumers that never provisioned a queue.
    """
    if not instance.queue_created or not instance.rmq_username:
        return

    rmq_backend = backend.RabbitMQManagementBackend()
    try:
        rmq_backend.delete_queue(instance.vhost, instance.queue_name)
    except Exception as e:
        logger.warning(
            "Failed to delete RabbitMQ queue %s for consumer %s: %s",
            instance.queue_name,
            instance,
            e,
        )
    try:
        rmq_backend.delete_rabbitmq_user(instance.rmq_username)
    except Exception as e:
        logger.warning(
            "Failed to delete RabbitMQ user %s for consumer %s: %s",
            instance.rmq_username,
            instance,
            e,
        )
