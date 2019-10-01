import logging
import datetime

from django.db import transaction, IntegrityError
from django.db.models import Q
from django.core import exceptions as django_exceptions

from waldur_core.core.utils import month_start
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_mastermind.slurm_invoices import models as slurm_invoices_models
from waldur_slurm import models as slurm_models
from waldur_slurm.apps import SlurmConfig

from . import utils

logger = logging.getLogger(__name__)


def synchronize_slurm_package(sender, instance, created=False, **kwargs):
    plan = instance.plan

    if not created and not set(instance.tracker.changed()) & {'amount', 'price'}:
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

    if created:
        with transaction.atomic():
            slurm_package, _ = slurm_invoices_models.SlurmPackage.objects.get_or_create(
                service_settings=plan.offering.scope,
                defaults=dict(
                    name=plan.name,
                    product_code=plan.product_code,
                    article_code=plan.article_code,
                    cpu_price=prices.get('cpu'),
                    gpu_price=prices.get('gpu'),
                    ram_price=prices.get('ram'),
                )
            )
            plan.scope = slurm_package
            plan.save()
    elif plan.scope:
        plan.scope.cpu_price = prices.get('cpu')
        plan.scope.gpu_price = prices.get('gpu')
        plan.scope.ram_price = prices.get('ram')
        plan.scope.save(update_fields=['cpu_price', 'gpu_price', 'ram_price'])


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
            plan_period = marketplace_models.ResourcePlanPeriod.objects. \
                filter(Q(start__lte=date) | Q(start__isnull=True)). \
                filter(Q(end__gt=date) | Q(end__isnull=True)). \
                get(resource=resource)
            marketplace_models.ComponentUsage.objects.create(
                resource=resource,
                component=plan_component,
                usage=usage,
                date=date,
                billing_period=month_start(date),
                plan_period=plan_period,
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


def add_component_usage(sender, instance, created=False, **kwargs):
    component_usage = instance

    if not created and not component_usage.tracker.has_changed('usage'):
        return

    if not isinstance(component_usage.resource.scope, slurm_models.Allocation):
        return

    utils.component_usage_register(component_usage)
