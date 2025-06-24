import datetime
import decimal
import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.log import event_logger
from waldur_core.structure.models import Project
from waldur_mastermind.invoices import signals as cost_signals
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates

from . import models, registrators
from .models import CustomerCredit, Invoice, InvoiceItem

logger = logging.getLogger(__name__)


def log_invoice_state_transition(
    sender, instance: models.Invoice, created=False, **kwargs
):
    if created:
        return

    state = instance.state
    if state == models.Invoice.States.PENDING or state == instance.tracker.previous(
        "state"
    ):
        return

    if state == models.Invoice.States.CREATED:
        event_logger.info(
            "Invoice for customer {customer_name} has been created.",
            event_type="invoice_created",
            event_context={
                "month": instance.month,
                "year": instance.year,
                "customer": instance.customer,
                "invoice": instance,
            },
            group="invoice",
        )
    elif state == models.Invoice.States.PAID:
        event_logger.info(
            "Invoice for customer {customer_name} has been paid.",
            event_type="invoice_paid",
            event_context={
                "month": instance.month,
                "year": instance.year,
                "customer": instance.customer,
                "invoice": instance,
            },
            group="invoice",
        )
    elif state == models.Invoice.States.CANCELED:
        event_logger.info(
            "Invoice for customer {customer_name} has been canceled.",
            event_type="invoice_canceled",
            event_context={
                "month": instance.month,
                "year": instance.year,
                "customer": instance.customer,
                "invoice": instance,
            },
            group="invoice",
        )


def set_tax_percent_on_invoice_creation(sender, instance: Invoice, **kwargs):
    if instance.pk is not None:
        return

    instance.tax_percent = instance.customer.default_tax_percent


def set_project_name_on_invoice_item_creation(
    sender, instance: InvoiceItem, created=False, **kwargs
):
    if created and instance.project:
        item = instance
        item.project_name = item.project.name
        item.project_uuid = item.project.uuid.hex
        item.save(update_fields=("project_name", "project_uuid"))


def update_invoice_item_on_project_name_update(sender, instance: Project, **kwargs):
    project = instance

    if not project.tracker.has_changed("name"):
        return

    query = Q(project=project, invoice__state=models.Invoice.States.PENDING)
    for item in models.InvoiceItem.objects.filter(query).only("pk"):
        item.project_name = project.name
        item.save(update_fields=["project_name"])


def emit_invoice_created_event(sender, instance: Invoice, created=False, **kwargs):
    if created:
        return

    state = instance.state
    if state != models.Invoice.States.CREATED or state == instance.tracker.previous(
        "state"
    ):
        return

    cost_signals.invoice_created.send(
        sender=models.Invoice,
        invoice=instance,
        issuer_details=settings.WALDUR_INVOICES["ISSUER_DETAILS"],
    )


def update_cache_when_invoice_item_is_updated(
    sender, instance: InvoiceItem, created=False, **kwargs
):
    invoice_item = instance
    if created or set(invoice_item.tracker.changed()) & {
        "start",
        "end",
        "quantity",
        "unit_price",
    }:
        transaction.on_commit(lambda: invoice_item.invoice.update_cache())


def update_cache_when_invoice_item_is_deleted(sender, instance: InvoiceItem, **kwargs):
    def update_invoice():
        try:
            instance.invoice.update_cache()
        except ObjectDoesNotExist:
            # It is okay to skip cache invalidation if invoice has been already removed
            pass

    transaction.on_commit(update_invoice)


def projects_customer_has_been_changed(
    sender, project, old_customer, new_customer, created=False, **kwargs
):
    try:
        today = timezone.now()
        date = core_utils.month_start(today)

        invoice = models.Invoice.objects.get(
            customer=old_customer,
            state=models.Invoice.States.PENDING,
            month=date.month,
            year=date.year,
        )
    except models.Invoice.DoesNotExist:
        return

    new_invoice, create = registrators.RegistrationManager.get_or_create_invoice(
        new_customer, date
    )

    if create:
        invoice.items.filter(project=project).delete()
    else:
        invoice.items.filter(project=project).update(invoice=new_invoice)


def create_recurring_usage_if_invoice_has_been_created(
    sender, instance: Invoice, created=False, **kwargs
):
    if not created:
        return

    invoice = instance

    now = timezone.now()
    prev_month = (now.replace(day=1) - datetime.timedelta(days=1)).date()
    prev_month_start = prev_month.replace(day=1)
    usages = marketplace_models.ComponentUsage.objects.filter(
        resource__project__customer=invoice.customer,
        recurring=True,
        billing_period__gte=prev_month_start,
    ).exclude(resource__state=ResourceStates.TERMINATED)

    if not usages:
        return

    for usage in usages:
        marketplace_models.ComponentUsage.objects.update_or_create(
            resource=usage.resource,
            component=usage.component,
            plan_period=usage.plan_period,
            billing_period=core_utils.month_start(now),
            defaults={
                "usage": usage.usage,
                "date": now,
                "description": usage.description,
                "recurring": usage.recurring,
                "modified_by": usage.modified_by,
            },
        )


def log_credit(sender, instance: CustomerCredit, created=False, **kwargs):
    credit = instance

    if created:
        event_logger.info(
            "{customer_name} credit has been created. Value: {new_value}",
            event_type="create_of_credit_by_staff",
            event_context={
                "new_value": int(credit.value),
                "customer": credit.customer,
            },
            group="credit",
        )
    elif credit.tracker.has_changed("value"):
        event_logger.info(
            "{customer_name} credit has been updated from {old_value} to {new_value}. ",
            event_type="update_of_credit_by_staff",
            event_context={
                "new_value": int(credit.value),
                "old_value": int(credit.tracker.previous("value")),
                "customer": credit.customer,
            },
            group="credit",
        )


def log_invoice_item_save(
    sender, instance: models.InvoiceItem, created=False, **kwargs
):
    if created:
        event_logger.info(
            f"Invoice item {instance.name} has been created.",
            event_type="invoice_item_created",
            event_context={
                "invoice_item": instance,
            },
            group="invoice_item",
        )
    else:

        def values_different(old, new):
            # Handle None values
            if old is None or new is None:
                return old is not new

            # Convert both to same type if one is string and other is numeric
            try:
                if isinstance(old, str) and isinstance(new, (float | decimal.Decimal)):
                    old = float(old)
                elif isinstance(new, str) and isinstance(
                    old, (float | decimal.Decimal)
                ):
                    new = float(new)
            except (ValueError, TypeError):
                # If conversion fails, fall back to string comparison
                return str(old).strip() != str(new).strip()

            # Handle numeric comparisons with tolerance
            if isinstance(old, (float | decimal.Decimal)) and isinstance(
                new, (float | decimal.Decimal)
            ):
                return abs(float(old) - float(new)) > 1e-10

            # Handle string comparisons
            if isinstance(old, str) and isinstance(new, str):
                return old.strip() != new.strip()

            # Default comparison for other types
            return old != new

        changes = [
            f"{field}: {instance.tracker.previous(field)} -> {getattr(instance, field, None)}"
            for field in instance.tracker.changed()
            if field != "details"
            and values_different(
                instance.tracker.previous(field), getattr(instance, field, None)
            )
        ]
        if changes:
            diff = ", ".join(changes)
            event_logger.info(
                f"Invoice item {instance.name} has been updated. Details: {diff}.",
                event_type="invoice_item_updated",
                event_context={
                    "invoice_item": instance,
                },
                group="invoice_item",
            )


def log_invoice_item_delete(sender, instance: models.InvoiceItem, **kwargs):
    event_logger.info(
        f"Invoice item {instance.name} has been deleted.",
        event_type="invoice_item_deleted",
        event_context={
            "invoice_item": instance,
        },
        group="invoice_item",
    )
