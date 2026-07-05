import datetime
import decimal
import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.models import Project
from waldur_mastermind.invoices import ledger, models
from waldur_mastermind.invoices import signals as cost_signals
from waldur_mastermind.invoices import utils as invoice_utils
from waldur_mastermind.invoices.audit import credit_audit_skipped, skip_credit_audit
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.enums import ResourceStates

from .models import CustomerCredit, Invoice, InvoiceItem, ProjectCredit

logger = logging.getLogger(__name__)

# Re-export so existing call sites can `from waldur_mastermind.invoices.handlers
# import skip_credit_audit` if they prefer the shorter import path.
__all__ = ["skip_credit_audit"]


def log_invoice_state_transition(
    sender, instance: models.Invoice, created=False, **kwargs
):
    if created:
        return

    state = instance.state
    if state == instance.tracker.previous("state"):
        return

    if state == models.Invoice.States.PENDING:
        return

    if state == models.Invoice.States.PENDING_FINALIZATION:
        return

    if state == models.Invoice.States.CREATED:
        event_logger.emit(
            "Invoice for customer {customer_name} has been created.",
            event_type=EventType.INVOICE_CREATED,
            event_context={
                "month": instance.month,
                "year": instance.year,
                "customer": instance.customer,
                "invoice": instance,
            },
            scopes=[instance, instance.customer],
        )
    elif state == models.Invoice.States.PAID:
        event_logger.emit(
            "Invoice for customer {customer_name} has been paid.",
            event_type=EventType.INVOICE_PAID,
            event_context={
                "month": instance.month,
                "year": instance.year,
                "customer": instance.customer,
                "invoice": instance,
            },
            scopes=[instance, instance.customer],
        )
    elif state == models.Invoice.States.CANCELED:
        event_logger.emit(
            "Invoice for customer {customer_name} has been canceled.",
            event_type=EventType.INVOICE_CANCELED,
            event_context={
                "month": instance.month,
                "year": instance.year,
                "customer": instance.customer,
                "invoice": instance,
            },
            scopes=[instance, instance.customer],
        )


def set_tax_percent_on_invoice_creation(sender, instance: Invoice, **kwargs):
    if instance.pk is not None:
        return

    if not instance.tax_percent:
        instance.tax_percent = instance.customer.default_tax_percent


def set_project_name_on_invoice_item_creation(
    sender, instance: InvoiceItem, created=False, **kwargs
):
    if created and instance.project:
        # Use ``QuerySet.update`` instead of ``instance.save`` to avoid
        # firing a second ``post_save`` for the same row — the recursive
        # signal would re-trigger every InvoiceItem handler (policy
        # triggers in particular) for what is essentially a denormalised
        # field write. Keep the in-memory instance in sync with the DB.
        project_name = instance.project.name
        project_uuid = instance.project.uuid.hex
        type(instance).objects.filter(pk=instance.pk).update(
            project_name=project_name,
            project_uuid=project_uuid,
        )
        instance.project_name = project_name
        instance.project_uuid = project_uuid


def update_invoice_item_on_project_name_update(sender, instance: Project, **kwargs):
    project = instance

    if not project.tracker.has_changed("name"):
        return

    query = Q(project=project, invoice__state__in=models.Invoice.States.MUTABLE_STATES)
    for item in models.InvoiceItem.objects.filter(query).only("pk"):
        item.project_name = project.name
        item.save(update_fields=["project_name"])


def emit_invoice_created_event(sender, instance: Invoice, created=False, **kwargs):
    """Emit invoice created signal when invoice state changes to CREATED."""
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
            state__in=models.Invoice.States.MUTABLE_STATES,
            month=date.month,
            year=date.year,
        )
    except models.Invoice.DoesNotExist:
        return

    new_invoice, create = MarketplaceBillingService.get_or_create_invoice(
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
    # Programmatic flows that emit their own specialized credit-mutation event
    # opt out via the skip_credit_audit() context manager to avoid duplicate
    # entries. Any other save (manual UI, REST API, shell, integrations) is
    # audited unconditionally — even when the caller passes update_fields.
    if credit_audit_skipped():
        return

    if kwargs.get("update_fields") and "value" not in kwargs["update_fields"]:
        # Save targeted fields other than `value` (e.g. expected_consumption only)
        # — nothing to audit here.
        return

    credit = instance

    if created:
        event_logger.emit(
            "{customer_name} credit has been created. Value: {new_value}",
            event_type=EventType.CREATE_OF_CREDIT_BY_STAFF,
            event_context={
                "new_value": int(credit.value),
                "customer": credit.customer,
            },
            scopes=[credit.customer],
        )
    elif credit.tracker.has_changed("value"):
        event_logger.emit(
            "{customer_name} credit has been updated from {old_value} to {new_value}. ",
            event_type=EventType.UPDATE_OF_CREDIT_BY_STAFF,
            event_context={
                "new_value": int(credit.value),
                "old_value": int(credit.tracker.previous("value")),
                "customer": credit.customer,
            },
            scopes=[credit.customer],
        )


def log_project_credit(sender, instance: ProjectCredit, created=False, **kwargs):
    # See log_credit() for the rationale behind opt-in suppression.
    if credit_audit_skipped():
        return

    if kwargs.get("update_fields") and "value" not in kwargs["update_fields"]:
        # Save targeted fields other than `value` — nothing to audit here.
        return

    credit = instance

    if created:
        event_logger.emit(
            "{project_name} project credit has been created. Value: {new_value}",
            event_type=EventType.CREATE_OF_PROJECT_CREDIT_BY_STAFF,
            event_context={
                "new_value": int(credit.value),
                "customer": credit.project.customer,
                "project": credit.project,
            },
            scopes=[credit.project.customer],
        )
    elif credit.tracker.has_changed("value"):
        event_logger.emit(
            "{project_name} project credit has been updated from {old_value} to {new_value}.",
            event_type=EventType.UPDATE_OF_PROJECT_CREDIT_BY_STAFF,
            event_context={
                "new_value": int(credit.value),
                "old_value": int(credit.tracker.previous("value")),
                "customer": credit.project.customer,
                "project": credit.project,
            },
            scopes=[credit.project.customer],
        )


def log_invoice_item_save(
    sender, instance: models.InvoiceItem, created=False, **kwargs
):
    if created:
        event_logger.emit(
            f"Invoice item {instance.name} has been created.",
            event_type=EventType.INVOICE_ITEM_CREATED,
            event_context={
                "invoice_item": instance,
            },
            scopes=[instance.invoice, instance.invoice.customer],
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
            event_logger.emit(
                f"Invoice item {instance.name} has been updated. Details: {diff}.",
                event_type=EventType.INVOICE_ITEM_UPDATED,
                event_context={
                    "invoice_item": instance,
                },
                scopes=[instance.invoice, instance.invoice.customer],
            )


def log_invoice_item_delete(sender, instance: models.InvoiceItem, **kwargs):
    event_logger.emit(
        f"Invoice item {instance.name} has been deleted.",
        event_type=EventType.INVOICE_ITEM_DELETED,
        event_context={
            "invoice_item": instance,
        },
        scopes=[instance.invoice, instance.invoice.customer],
    )


def refund_project_credit_on_project_removal(sender, instance: Project, **kwargs):
    project = instance

    project_credit = models.ProjectCredit.objects.filter(project=project).first()

    if not project_credit:
        return

    customer_credit = models.CustomerCredit.objects.filter(
        customer=project.customer
    ).first()

    if not customer_credit:
        return

    if project_credit.value <= 0:
        return

    with (
        skip_credit_audit(),
        ledger.credit_transaction_type(
            models.CreditTransaction.Types.ADJUSTMENT, reference=project_credit
        ),
    ):
        if project_credit.mark_unused_credit_as_spent_on_project_termination:
            old_org_value = int(customer_credit.value or 0)
            if customer_credit.value > project_credit.value:
                customer_credit.value -= project_credit.value
            else:
                customer_credit.value = 0
            customer_credit.save(update_fields=["value"])
            event_logger.emit(
                "Organization credit has been decreased due to project removal.",
                event_type=EventType.AUTOMATIC_CREDIT_ADJUSTMENT,
                event_context={
                    "new_value": int(customer_credit.value or 0),
                    "old_value": old_org_value,
                    "customer": customer_credit.customer,
                },
                scopes=[customer_credit.customer],
            )

        if project_credit.value != 0:
            old_value = int(project_credit.value)
            project_credit.value = 0
            project_credit.save(update_fields=["value"])
            event_logger.emit(
                "Project credit has been set to 0 on project removal.",
                event_type=EventType.AUTOMATIC_CREDIT_ADJUSTMENT,
                event_context={
                    "new_value": 0,
                    "old_value": old_value,
                    "customer": project.customer,
                },
                scopes=[project.customer],
            )


def record_credit_transaction(
    sender, instance: CustomerCredit, created=False, **kwargs
):
    """Write a CreditTransaction ledger row for every CustomerCredit value
    change. The semantic type comes from the innermost
    ``ledger.credit_transaction_type`` block; untyped mutations (staff UI,
    REST API, shell) are recorded as staff grants. Unlike the audit events,
    ledger writes are never suppressed — the withdrawable balance is
    derived from them.
    """
    update_fields = kwargs.get("update_fields")
    if update_fields and "value" not in update_fields:
        return

    if created:
        delta = instance.value
    elif instance.tracker.has_changed("value"):
        previous = instance.tracker.previous("value") or decimal.Decimal("0")
        delta = instance.value - previous
    else:
        return

    if not delta:
        return

    transaction_type, reference, comment = ledger.current_credit_transaction_type()
    models.CreditTransaction.objects.create(
        credit=instance,
        amount=delta,
        transaction_type=transaction_type or models.CreditTransaction.Types.STAFF_GRANT,
        reference=reference,
        comment=comment or "",
    )


AFFILIATE_TERM_FIELDS = (
    "fee_percent",
    "is_active",
    "start_date",
    "end_date",
)


def log_affiliate(sender, instance: models.CustomerAffiliate, created=False, **kwargs):
    """Audit staff changes of affiliate terms. Scoped to the affiliate
    organization so affiliates can see the history of their own terms.
    """
    link = instance

    # Customer names and the diff are passed as context VALUES (not woven
    # into the template) so event_logger's .format() never tries to resolve
    # braces that happen to appear in user-controlled data.
    if created:
        event_logger.emit(
            "Affiliate terms of {customer_name} have been created: "
            "{fee_percent}% fee for invoices of {referred_customer_name}.",
            event_type=EventType.CREATE_OF_AFFILIATE_BY_STAFF,
            event_context={
                "customer": link.affiliate,
                "referred_customer_name": link.customer.name,
                "fee_percent": str(link.fee_percent),
            },
            scopes=[link.affiliate],
        )
        return

    changes = {
        field: (link.tracker.previous(field), getattr(link, field))
        for field in AFFILIATE_TERM_FIELDS
        if link.tracker.has_changed(field)
    }
    if not changes:
        return

    diff = ", ".join(
        f"{field}: {old} -> {new}" for field, (old, new) in changes.items()
    )
    event_logger.emit(
        "Affiliate terms of {customer_name} have been updated. Details: {details}.",
        event_type=EventType.UPDATE_OF_AFFILIATE_BY_STAFF,
        event_context={
            "customer": link.affiliate,
            "referred_customer_name": link.customer.name,
            "details": diff,
            "fee_percent": str(link.fee_percent),
        },
        scopes=[link.affiliate],
    )


def process_affiliate_fees(sender, invoice: Invoice, **kwargs):
    """Accrue affiliate fees when an invoice is finalized.

    Runs after credit compensations (the invoice_created signal fires on
    the PENDING -> CREATED transition), so fees are computed on the
    post-compensation net price. Any per-link failure is logged and
    skipped: fee accrual must never block invoice finalization.
    """
    if not invoice_utils.affiliates_feature_enabled():
        return

    period = datetime.date(int(invoice.year), int(invoice.month), 1)
    links = models.CustomerAffiliate.objects.filter(
        customer=invoice.customer, is_active=True
    )
    for link in links:
        if not link.is_active_on(period):
            continue
        try:
            accrue_affiliate_fee(link, invoice)
        except Exception:
            logger.exception(
                "Failed to accrue affiliate fee for link %s and invoice %s.",
                link.uuid,
                invoice.uuid,
            )


def accrue_affiliate_fee(link: models.CustomerAffiliate, invoice: Invoice):
    amount = invoice.price
    if amount <= 0:
        return

    fee = link.calculate_fee(amount)
    if fee <= 0:
        return

    with transaction.atomic():
        if models.AffiliateFeeAccrual.objects.filter(
            affiliate_link=link, invoice=invoice
        ).exists():
            # Finalization tasks may re-run (grace period, retries) —
            # the fee for this invoice has already been accrued.
            return

        credit, _ = models.CustomerCredit.objects.select_for_update().get_or_create(
            customer=link.affiliate
        )
        accrual = models.AffiliateFeeAccrual.objects.create(
            affiliate_link=link, invoice=invoice, amount=fee
        )
        # The accrual emits its own specialized event below; type the ledger
        # row so the fee counts towards the withdrawable balance.
        with (
            skip_credit_audit(),
            ledger.credit_transaction_type(
                models.CreditTransaction.Types.AFFILIATE_FEE, reference=accrual
            ),
        ):
            credit.value += fee
            credit.save(update_fields=["value"])

    event_logger.emit(
        "{customer_name} earned an affiliate fee of {amount} for period {period}.",
        event_type=EventType.INCREASE_OF_CUSTOMER_CREDIT_DUE_TO_AFFILIATE_FEE,
        event_context={
            # Privacy: period and amount only — never invoice details of the
            # referred customer.
            "customer": link.affiliate,
            "amount": float(fee),
            "period": f"{invoice.year}-{int(invoice.month):02d}",
            "credit_balance": int(credit.value),
        },
        scopes=[link.affiliate],
    )
