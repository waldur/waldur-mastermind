import logging
import datetime

from django.db import transaction, IntegrityError
from django.core import exceptions as django_exceptions

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_mastermind.slurm_invoices import models as slurm_invoices_models
from waldur_slurm.apps import SlurmConfig

logger = logging.getLogger(__name__)


def create_slurm_package(sender, instance, created=False, **kwargs):
    plan = instance.plan

    if not created:
        return

    if plan.offering.type != PLUGIN_NAME:
        return

    if not isinstance(plan.offering.scope, structure_models.ServiceSettings):
        logger.warning('Skipping plan synchronization because offering scope is not service settings. '
                       'Plan ID: %s', plan.id)
        return

    if plan.offering.scope.type != SlurmConfig.service_name:
        logger.warning('Skipping plan synchronization because service settings type is not SLURM. '
                       'Plan ID: %s', plan.id)
        return

    expected_types = set(manager.get_component_types(PLUGIN_NAME))
    actual_types = set(plan.components.values_list('component__type', flat=True))
    if expected_types != actual_types:
        return

    prices = {component.component.type: component.price
              for component in plan.components.all()}

    with transaction.atomic():
        slurm_package = slurm_invoices_models.SlurmPackage.objects.create(
            service_settings=plan.offering.scope,
            name=plan.name,
            product_code=plan.product_code,
            article_code=plan.article_code,
            cpu_price=prices.get('cpu'),
            gpu_price=prices.get('gpu'),
            ram_price=prices.get('ram'),
        )
        plan.scope = slurm_package
        plan.save()


def create_slurm_usage(sender, instance, created=False, **kwargs):
    allocation_usage = instance
    allocation = allocation_usage.allocation

    try:
        resource = marketplace_models.Resource.objects.get(scope=allocation)
    except django_exceptions.ObjectDoesNotExist:
        return

    date = datetime.date(year=allocation_usage.year, month=allocation_usage.month, day=1)

    for component in manager.get_components(PLUGIN_NAME):
        usage = getattr(allocation_usage, component.type + '_usage')

        try:
            plan_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering,
                type=component.type
            )
            marketplace_models.ComponentUsage.objects.create(
                resource=resource,
                component=plan_component,
                usage=usage,
                date=date,
            )
        except django_exceptions.ObjectDoesNotExist:
            logger.warning('Skipping AllocationUsage synchronization because this '
                           'marketplace.OfferingComponent does not exist.'
                           'AllocationUsage ID: %s', allocation_usage.id)
        except IntegrityError:
            logger.warning('Skipping AllocationUsage synchronization because this marketplace.ComponentUsage exists.'
                           'AllocationUsage ID: %s', allocation_usage.id)


def update_component_quota(sender, instance, created=False, **kwargs):
    if created:
        return

    allocation = instance

    try:
        resource = marketplace_models.Resource.objects.get(scope=allocation)
    except django_exceptions.ObjectDoesNotExist:
        return

    for component in manager.get_components(PLUGIN_NAME):
        usage = getattr(allocation, component.type + '_usage')
        limit = getattr(allocation, component.type + '_limit')

        try:
            plan_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering,
                type=component.type
            )
            component_quota = marketplace_models.ComponentQuota.objects.get(
                resource=resource,
                component=plan_component,
            )
            component_quota.limit = limit
            component_quota.usage = usage
            component_quota.save()

        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning('Skipping Allocation synchronization because this '
                           'marketplace.OfferingComponent does not exist.'
                           'Allocation ID: %s', allocation.id)
        except marketplace_models.ComponentQuota.DoesNotExist:
            marketplace_models.ComponentQuota.objects.create(
                resource=resource,
                component=plan_component,
                limit=limit,
                usage=usage
            )


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created or not instance.tracker.has_changed('state'):
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except django_exceptions.ObjectDoesNotExist:
        logger.warning('Skipping SLURM allocation state synchronization '
                       'because related resource is not found. Allocation ID: %s', instance.id)
    else:
        callbacks.sync_resource_state(instance, resource)


def terminate_resource(sender, instance, **kwargs):
    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except django_exceptions.ObjectDoesNotExist:
        logger.debug('Skipping resource terminate for SLURM allocation '
                     'because related resource does not exist. '
                     'Allocation ID: %s', instance.id)
    else:
        callbacks.resource_deletion_succeeded(resource)
