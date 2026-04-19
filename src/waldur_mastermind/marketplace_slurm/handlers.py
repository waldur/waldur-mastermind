import logging

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import SLURM_OFFERING
from waldur_slurm.models import Allocation, AllocationUserUsage

logger = logging.getLogger(__name__)

COMPONENT_FIELDS = {
    "cpu_usage",
    "gpu_usage",
    "ram_usage",
    "cpu_limit",
    "gpu_limit",
    "ram_limit",
}


def update_component_quota(sender, instance: Allocation, created=False, **kwargs):
    if created:
        return

    if not set(instance.tracker.changed()) & COMPONENT_FIELDS:
        return

    marketplace_utils.update_component_quota(instance, SLURM_OFFERING)


def create_offering_user_for_slurm_user(sender, allocation, user, username, **kwargs):
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping SLURM user synchronization because offering is not found. "
            "SLURM settings ID: %s",
            allocation.service_settings_id,
        )
        return

    marketplace_models.OfferingUser.objects.update_or_create(
        offering=offering,
        user=user,
        defaults={"username": username},
    )


def drop_offering_user_for_slurm_user(sender, allocation, user, **kwargs):
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping SLURM user synchronization because offering is not found. "
            "SLURM settings ID: %s",
            allocation.service_settings_id,
        )
        return

    marketplace_models.OfferingUser.objects.filter(
        offering=offering, user=user
    ).delete()


def sync_component_user_usage_when_allocation_user_usage_is_submitted(
    sender, instance: AllocationUserUsage, **kwargs
):
    marketplace_utils.sync_component_user_usage(instance, SLURM_OFFERING)
