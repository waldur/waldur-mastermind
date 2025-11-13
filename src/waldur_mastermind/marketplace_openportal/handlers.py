import logging

from django.core import exceptions as django_exceptions
from django.utils import timezone

from waldur_core.core.utils import month_start
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
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

    allocation = instance

    try:
        resource = marketplace_models.Resource.objects.get(scope=allocation)
    except django_exceptions.ObjectDoesNotExist:
        return

    new_limits = {}
    new_usages = {}
    for component in manager.get_components(PLUGIN_NAME):
        usage = float(getattr(allocation, component.type + "_usage"))
        limit = float(getattr(allocation, component.type + "_limit"))

        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type=component.type
            )
        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning(
                "Skipping Allocation synchronization because this "
                "marketplace.OfferingComponent does not exist."
                "Allocation ID: %s",
                allocation.id,
            )
        else:
            new_limits[component.type] = limit
            new_usages[component.type] = usage
            marketplace_models.ComponentQuota.objects.update_or_create(
                resource=resource,
                component=offering_component,
                defaults={"limit": limit, "usage": usage},
            )

            plan_period = marketplace_models.ResourcePlanPeriod.objects.filter(
                resource=resource, end=None
            )

            if not plan_period.exists():
                logger.warning(
                    "Skipping component usage synchronization because valid "
                    "ResourcePlanPeriod is not found. "
                    f"Allocation: {allocation}, Resource: {resource}",
                )
                continue

            if plan_period.count() > 1:
                logger.warning(
                    f"More than one active ResourcePlanPeriod found for Allocation: {allocation}, Resource: {resource}. "
                    "Using the first plan only."
                )

            plan_period = plan_period.first()

            date = timezone.now()
            marketplace_models.ComponentUsage.objects.update_or_create(
                resource=resource,
                component=offering_component,
                billing_period=month_start(date),
                plan_period=plan_period,
                defaults={"usage": usage, "date": date},
            )

    logger.info(f"Old limits: {resource.limits}, new limits: {new_limits}")

    if resource.limits != new_limits:
        logger.debug(
            f"Syncing limits for OpenPortal. Allocation: {allocation}. Old limits: {resource.limits}. New limits: {new_limits}",
        )
        resource.limits = new_limits
        resource.save(update_fields=["limits"])

    if resource.current_usages != new_usages:
        logger.debug(
            f"Syncing usages for OpenPortal. Allocation: {allocation}. Old usages: {resource.current_usages}. New usages: {new_usages}",
        )
        resource.current_usages = new_usages
        resource.save(update_fields=["current_usages"])


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
