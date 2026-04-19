import logging

from django.db.models.signals import post_save

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace_openportal_remote import PLUGIN_NAME

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


def create_offering_user_for_openportal_remote_user(sender, allocation, user, **kwargs):
    logger.info(
        f"OpenPortal Remote - creating offering user for user {user} in {allocation}"
    )
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping OpenPortal Remote user synchronization because offering is not found. "
            "OpenPortal settings ID: %s",
            allocation.service_settings_id,
        )
        return

    marketplace_models.OfferingUser.objects.update_or_create(
        offering=offering,
        user=user,
    )


def drop_offering_user_for_openportal_remote_user(sender, allocation, user, **kwargs):
    logger.info(
        f"OpenPortal Remote - dropping offering user for user {user} in {allocation}"
    )
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping OpenPortal Remote user synchronization because offering is not found. "
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


def sync_offering_resource_options(sender, instance, **kwargs):
    logger.debug(
        f"Synchronizing OpenPortal Remote resource options for Offering {instance}"
    )

    offering: marketplace_models.Offering = instance
    if offering.type != PLUGIN_NAME:
        logger.debug(f"Skipping as {offering.type} is not {PLUGIN_NAME}")
        return

    # check to make sure that the offering has the right resource_options
    is_dirty = False

    if (
        offering.resource_options is None
        or not isinstance(offering.resource_options, dict)
        or not isinstance(offering.resource_options.get("options"), dict)
        or not isinstance(offering.resource_options.get("order"), list)
    ):
        # this is the default from Waldur
        is_dirty = True
        offering.resource_options = {"options": {}, "order": []}

    if "allocation" not in offering.resource_options["order"]:
        # add allocation to the order
        offering.resource_options["order"].append("allocation")

    # The service settings are held in the offerings scope
    max_allocation = None

    if offering.scope:
        try:
            max_allocation = offering.scope.get_option("max_allocation")
        except Exception as e:
            logger.error(f"Error getting max_allocation from scope: {e}")

    allocation_options = {
        "type": "integer",
        "label": "Allocation",
        "help_text": "Allocation in resource units",
        "required": False,
        "min": 0,
    }

    if max_allocation is not None:
        try:
            allocation_options["max"] = int(max_allocation)
        except (ValueError, TypeError) as e:
            logger.error(f"Error converting max_allocation to int: {e}.")

    if "options" not in offering.resource_options:
        # initialize options if not present
        is_dirty = True
        offering.resource_options["options"] = {}

    if (
        is_dirty
        or offering.resource_options["options"].get("allocation") != allocation_options
    ):
        # update allocation options if they are different
        logger.info(
            f"Updating allocation options for offering {offering.uuid} with {allocation_options}"
        )

        offering.resource_options["options"]["allocation"] = allocation_options

        # 1. Disconnect the receiver
        post_save.disconnect(
            sync_offering_resource_options,
            sender=marketplace_models.Offering,
        )

        try:
            # 2. Save the instance
            offering.save(update_fields=["resource_options"])
        finally:
            # 3. Reconnect the receiver
            post_save.connect(
                sync_offering_resource_options,
                sender=marketplace_models.Offering,
            )
