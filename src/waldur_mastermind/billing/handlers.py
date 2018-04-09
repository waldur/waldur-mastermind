from __future__ import unicode_literals

import logging

from django.db import transaction

from waldur_core.structure import models as structure_models

from . import log, models

logger = logging.getLogger(__name__)


def create_price_estimate(sender, instance, created=False, **kwargs):
    if not created:
        return
    models.PriceEstimate.objects.create(scope=instance)


def delete_stale_price_estimate(sender, instance, **kwargs):
    models.PriceEstimate.objects.filter(scope=instance).delete()


def update_estimate_when_invoice_is_created(sender, instance, created=False, **kwargs):
    if not created:
        return
    transaction.on_commit(lambda: update_estimates_for_customer(instance.customer))


def update_estimates_for_customer(customer):
    scopes = [customer] + list(customer.projects.all())
    for scope in scopes:
        estimate, _ = models.PriceEstimate.objects.get_or_create(scope=scope)
        estimate.update_total()
        estimate.save(update_fields=['total'])


def process_invoice_item(sender, instance, created=False, **kwargs):
    if (not created and
            not instance.tracker.has_changed('unit_price') and
            not instance.tracker.has_changed('start') and
            not instance.tracker.has_changed('end')):
        return
    with transaction.atomic():
        for scope in [instance.project, instance.project.customer]:
            estimate, _ = models.PriceEstimate.objects.get_or_create(scope=scope)
            estimate.update_total()
            estimate.validate_limit()
            estimate.save(update_fields=['total'])


def log_price_estimate_limit_update(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('limit'):
        if isinstance(instance.scope, structure_models.Customer):
            event_type = 'project_price_limit_updated'
        elif isinstance(instance.scope, structure_models.Project):
            event_type = 'customer_price_limit_updated'
        else:
            logger.warning('A price estimate event for type of "%s" is not registered.', type(instance.scope))
            return

        message = 'Price limit for "%(scope)s" has been updated from "%(old)s" to "%(new)s".' % {
            'scope': instance.scope,
            'old': instance.tracker.previous('limit'),
            'new': instance.limit
        }
        log.event_logger.price_estimate.info(message, event_type=event_type)
