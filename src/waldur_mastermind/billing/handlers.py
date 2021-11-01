import logging

from django.db import transaction

from waldur_core.structure import models as structure_models

from . import models

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
    if not created and not set(instance.tracker.changed()) & {
        'unit_price',
        'start',
        'end',
        'quantity',
    }:
        return

    if not instance.project_id:
        return
    with transaction.atomic():
        project = structure_models.Project.all_objects.get(pk=instance.project_id)

        for scope in [project, project.customer]:
            estimate, _ = models.PriceEstimate.objects.get_or_create(scope=scope)
            estimate.update_total()
            estimate.save(update_fields=['total'])
