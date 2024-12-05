import logging

from django.db import transaction

from waldur_core.logging import models, tasks, utils

logger = logging.getLogger(__name__)


def process_hook(sender, instance, created=False, **kwargs):
    transaction.on_commit(lambda: tasks.process_event.delay(instance.pk))


def delete_stale_event_subscriptions(sender, instance, **kwargs):
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
