import logging

from django.core import exceptions as django_exceptions
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.utils import timezone

from waldur_core.core.utils import month_start
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME

logger = logging.getLogger(__name__)

COMPONENT_FIELDS = {
    'cpu_usage',
    'gpu_usage',
    'ram_usage',
    'cpu_limit',
    'gpu_limit',
    'ram_limit',
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
        usage = getattr(allocation, component.type + '_usage')
        limit = getattr(allocation, component.type + '_limit')

        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type=component.type
            )
        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning(
                'Skipping Allocation synchronization because this '
                'marketplace.OfferingComponent does not exist.'
                'Allocation ID: %s',
                allocation.id,
            )
        else:
            new_limits[component.type] = limit
            new_usages[component.type] = usage
            marketplace_models.ComponentQuota.objects.update_or_create(
                resource=resource,
                component=offering_component,
                defaults={'limit': limit, 'usage': usage},
            )
            try:
                plan_period = marketplace_models.ResourcePlanPeriod.objects.get(
                    resource=resource, end=None
                )
            except (ObjectDoesNotExist, MultipleObjectsReturned):
                logger.warning(
                    'Skipping component usage synchronization because valid'
                    'ResourcePlanPeriod is not found.'
                    'Allocation ID: %s',
                    allocation.id,
                )
            else:
                date = timezone.now()
                marketplace_models.ComponentUsage.objects.update_or_create(
                    resource=resource,
                    component=offering_component,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                    defaults={'usage': usage, 'date': date},
                )

    if resource.limits != new_limits:
        logger.debug(
            'Syncing limits for SLURM. Allocation ID: %s. Old limits: %s. New limits: %s',
            allocation.id,
            resource.limits,
            new_limits,
        )
        resource.limits = new_limits
        resource.save(update_fields=['limits'])

    if resource.current_usages != new_usages:
        logger.debug(
            'Syncing usages for SLURM. Allocation ID: %s. Old usages: %s. New usages: %s',
            allocation.id,
            resource.current_usages,
            new_usages,
        )
        resource.current_usages = new_usages
        resource.save(update_fields=['current_usages'])


def create_offering_user_for_slurm_user(sender, allocation, user, username, **kwargs):
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            'Skipping SLURM user synchronization because offering is not found. '
            'SLURM settings ID: %s',
            allocation.service_settings_id,
        )
        return

    marketplace_models.OfferingUser.objects.update_or_create(
        offering=offering,
        user=user,
        defaults={'username': username},
    )


def drop_offering_user_for_slurm_user(sender, allocation, user, **kwargs):
    try:
        offering = marketplace_models.Offering.objects.get(
            scope=allocation.service_settings
        )
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            'Skipping SLURM user synchronization because offering is not found. '
            'SLURM settings ID: %s',
            allocation.service_settings_id,
        )
        return

    marketplace_models.OfferingUser.objects.filter(
        offering=offering, user=user
    ).delete()
