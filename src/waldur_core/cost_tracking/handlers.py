from __future__ import unicode_literals

import logging

from celery import current_task
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.cost_tracking import models, CostTrackingRegister, ResourceNotRegisteredError
from waldur_core.structure import models as structure_models

logger = logging.getLogger(__name__)


def scope_deletion(sender, instance, **kwargs):
    """ Run different actions on price estimate scope deletion.

        If scope is a customer - delete all customer estimates and their children.
        If scope is a deleted resource - redefine consumption details, recalculate
                                         ancestors estimates and update estimate details.
        If scope is a unlinked resource - delete all resource price estimates and update ancestors.
        In all other cases - update price estimate details.
    """

    is_resource = isinstance(instance, structure_models.ResourceMixin)
    if is_resource and getattr(instance, 'PERFORM_UNLINK', False):
        _resource_unlink(resource=instance)
    elif is_resource and not getattr(instance, 'PERFORM_UNLINK', False):
        _resource_deletion(resource=instance)
    elif isinstance(instance, structure_models.Customer):
        _customer_deletion(customer=instance)
    else:
        for price_estimate in models.PriceEstimate.objects.filter(scope=instance):
            price_estimate.init_details()


def _resource_unlink(resource):
    if resource.__class__ not in CostTrackingRegister.registered_resources:
        return
    for price_estimate in models.PriceEstimate.objects.filter(scope=resource):
        price_estimate.update_ancestors_total(diff=-price_estimate.total)
        price_estimate.delete()


def _customer_deletion(customer):
    for estimate in models.PriceEstimate.objects.filter(scope=customer):
        for descendant in estimate.get_descendants():
            descendant.delete()


def _resource_deletion(resource):
    """ Recalculate consumption details and save resource details """
    if resource.__class__ not in CostTrackingRegister.registered_resources:
        return
    new_configuration = {}
    price_estimate = models.PriceEstimate.update_resource_estimate(resource, new_configuration)
    price_estimate.init_details()


def _is_in_celery_task():
    """ Return True if current code is executed in celery task """
    return bool(current_task)


def resource_update(sender, instance, created=False, **kwargs):
    """ Update resource consumption details and price estimate if its configuration has changed.
        Create estimates for previous months if resource was created not in current month.
    """
    resource = instance
    try:
        new_configuration = CostTrackingRegister.get_configuration(resource)
    except ResourceNotRegisteredError:
        return
    models.PriceEstimate.update_resource_estimate(
        resource, new_configuration, raise_exception=not _is_in_celery_task())
    # Try to create historical price estimates
    if created:
        _create_historical_estimates(resource, new_configuration)


def resource_quota_update(sender, instance, **kwargs):
    """ Update resource consumption details and price estimate if its configuration has changed """
    quota = instance
    resource = quota.scope
    try:
        new_configuration = CostTrackingRegister.get_configuration(resource)
    except ResourceNotRegisteredError:
        return
    models.PriceEstimate.update_resource_estimate(
        resource, new_configuration, raise_exception=not _is_in_celery_task())


def _create_historical_estimates(resource, configuration):
    """ Create consumption details and price estimates for past months.

        Usually we need to update historical values on resource import.
    """
    today = timezone.now()
    month_start = core_utils.month_start(today)
    while month_start > resource.created:
        month_start -= relativedelta(months=1)
        models.PriceEstimate.create_historical(resource, configuration, max(month_start, resource.created))
