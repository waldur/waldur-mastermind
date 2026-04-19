import logging

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace_openportal import PLUGIN_NAME

logger = logging.getLogger(__name__)

COMPONENT_FIELDS = {
    "node_usage",
    "node_limit",
}


def update_component_quota(sender, instance, created=False, **kwargs):
    if created:
        return

    if not set(instance.tracker.changed()) & COMPONENT_FIELDS:
        return

    marketplace_utils.update_component_quota(instance, PLUGIN_NAME)


def create_offering_user_for_openportal_user(sender, allocation, user, **kwargs):
    logger.info(f"OpenPortal - creating offering user for user {user} in {allocation}")
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping OpenPortal user synchronization because offering is not found. "
            "OpenPortal settings ID: %s",
            allocation.service_settings_id,
        )
        return

    marketplace_models.OfferingUser.objects.update_or_create(
        offering=offering,
        user=user,
    )


def drop_offering_user_for_openportal_user(sender, allocation, user, **kwargs):
    logger.info(f"OpenPortal - dropping offering user for user {user} in {allocation}")
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping OpenPortal user synchronization because offering is not found. "
            "OpenPortal settings ID: %s",
            allocation.service_settings_id,
        )
        return

    marketplace_models.OfferingUser.objects.filter(
        offering=offering, user=user
    ).delete()


def sync_component_user_usage_when_allocation_user_usage_is_submitted(
    sender, instance, **kwargs
):
    marketplace_utils.sync_component_user_usage(instance, PLUGIN_NAME)
