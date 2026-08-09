import logging

from django.db import transaction

from waldur_core.structure.models import Project
from waldur_mastermind.invoices.models import Invoice, InvoiceItem

from . import models

logger = logging.getLogger(__name__)


def create_price_estimate(sender, instance, created=False, **kwargs):
    """Create price estimate when customer or project is created."""
    if not created:
        return
    models.PriceEstimate.objects.create(scope=instance)


def delete_stale_price_estimate(sender, instance, **kwargs):
    """Delete price estimates when customer or project is deleted."""
    models.PriceEstimate.objects.filter(scope=instance).delete()


def update_estimate_when_invoice_is_created(
    sender, instance: Invoice, created=False, **kwargs
):
    """Update price estimates when new invoice is created for customer."""
    if not created:
        return
    transaction.on_commit(lambda: update_estimates_for_customer(instance.customer))


def update_estimates_for_scopes(scopes):
    """Recompute the stored total for each given customer or project.

    Each scope costs one aggregate over the month's invoice items, so callers
    that know which projects changed should pass only those.
    """
    for scope in scopes:
        estimate, _ = models.PriceEstimate.objects.get_or_create(scope=scope)
        estimate.update_total()
        estimate.save(update_fields=["total"])


def update_estimates_for_customer(customer):
    projects = list(Project.available_objects.filter(customer=customer))
    update_estimates_for_scopes([customer] + projects)


def process_invoice_item(sender, instance: InvoiceItem, created=False, **kwargs):
    """Process invoice item changes and update related price estimates."""
    if not created and not set(instance.tracker.changed()) & {
        "unit_price",
        "start",
        "end",
        "quantity",
    }:
        return

    if not instance.project:
        return
    with transaction.atomic():
        for scope in [instance.project, instance.project.customer]:
            estimate, _ = models.PriceEstimate.objects.get_or_create(scope=scope)
            estimate.update_total()
            estimate.save(update_fields=["total"])
