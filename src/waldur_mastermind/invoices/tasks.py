import datetime
import logging
from csv import DictWriter
from io import StringIO

from celery import shared_task
from constance import config
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.middleware import set_current_user
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices.utils import get_previous_month
from waldur_mastermind.marketplace import billing_discount
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.tasks import copy_future_price_to_current_price

from ..invoices import compensations, ledger, models, serializers, utils
from ..invoices.audit import skip_credit_audit

logger = logging.getLogger(__name__)


@shared_task(name="invoices.create_monthly_invoices")
def create_monthly_invoices():
    """
    - For every customer change state of the invoices for previous months from "pending" to "billed"
      and freeze their items (or transition to "pending_finalization" if grace period is configured).
    - Create new invoice for every customer in current month if not created yet.
    """
    set_current_user(core_utils.get_system_robot())
    copy_future_price_to_current_price()

    grace_hours = settings.WALDUR_INVOICES.get(
        "INVOICE_FINALIZATION_GRACE_PERIOD_HOURS", 0
    )

    local_date = timezone.localtime(timezone.now())
    old_invoices = models.Invoice.objects.filter(
        Q(state=models.Invoice.States.PENDING, year__lt=local_date.year)
        | Q(
            state=models.Invoice.States.PENDING,
            year=local_date.year,
            month__lt=local_date.month,
        )
    )

    if grace_hours == 0:
        # Backward compatible: finalize immediately
        set_to_zero_overdue_credits(local_date.date())
        for invoice in old_invoices:
            try:
                with transaction.atomic():
                    billing_discount.apply_aggregated_volume_discounts(invoice)
                    process_invoice_credits(invoice)
                    invoice.set_created()
            except Exception:
                logger.exception("Unable to process invoice %s", invoice)
                continue
    else:
        # Grace period: transition to PENDING_FINALIZATION
        for invoice in old_invoices:
            try:
                invoice.set_pending_finalization()
            except Exception:
                logger.exception(
                    "Unable to set pending_finalization for invoice %s", invoice
                )
                continue

    customers = structure_models.Customer.objects.exclude(archived=True)
    if settings.WALDUR_CORE["ENABLE_ACCOUNTING_START_DATE"]:
        customers = customers.filter(accounting_start_date__lt=timezone.now())

    for customer in customers:
        try:
            MarketplaceBillingService.get_or_create_invoice(
                customer, core_utils.month_start(local_date)
            )
        except Exception:
            # Continue processing even if some customers could not be processed
            logger.exception(
                "Unable to create monthly invoice for customer %s", customer
            )

    # Reports/notifications only if finalized immediately (grace_period=0)
    if grace_hours == 0:
        if settings.WALDUR_INVOICES["INVOICE_REPORTING"]["ENABLE"]:
            send_invoice_report.delay()
            send_monthly_invoicing_reports_about_customers.delay()

        if settings.WALDUR_INVOICES["SEND_CUSTOMER_INVOICES"]:
            send_new_invoices_notification.delay()


@shared_task(name="invoices.finalize_previous_invoices")
def finalize_previous_invoices():
    """
    Finalize invoices that are in PENDING_FINALIZATION state.

    Runs hourly on the 1st-3rd of each month. Checks whether the configured
    grace period has elapsed since midnight on the 1st before finalizing.
    No-op when there are no PENDING_FINALIZATION invoices or when the
    grace period has not yet elapsed.
    """
    set_current_user(core_utils.get_system_robot())
    pending_invoices = models.Invoice.objects.filter(
        state=models.Invoice.States.PENDING_FINALIZATION,
    )
    if not pending_invoices.exists():
        return

    grace_hours = settings.WALDUR_INVOICES.get(
        "INVOICE_FINALIZATION_GRACE_PERIOD_HOURS", 0
    )
    local_now = timezone.localtime(timezone.now())
    if grace_hours > 0:
        # Grace period is measured from midnight on the 1st of the current month
        month_start = local_now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        hours_since_month_start = (local_now - month_start).total_seconds() / 3600
        if hours_since_month_start < grace_hours:
            logger.info(
                "Grace period not yet elapsed (%.1f / %d hours). "
                "Skipping invoice finalization.",
                hours_since_month_start,
                grace_hours,
            )
            return

    # Use the 1st of current month as effective date, not today.
    # When grace period delays finalization (e.g. to Feb 2), credits with
    # end_date on the 1st must not be zeroed before compensations are applied.
    effective_date = local_now.replace(day=1).date()
    set_to_zero_overdue_credits(effective_date)
    for invoice in pending_invoices:
        try:
            with transaction.atomic():
                billing_discount.apply_aggregated_volume_discounts(invoice)
                process_invoice_credits(invoice)
                invoice.set_created()
        except Exception:
            logger.exception("Unable to finalize invoice %s", invoice)
            continue

    # Only send reports/notifications when all invoices have been finalized.
    # If some failed above, the next hourly run will finalize them and send them.
    remaining = models.Invoice.objects.filter(
        state=models.Invoice.States.PENDING_FINALIZATION,
    ).exists()

    if not remaining:
        if settings.WALDUR_INVOICES["INVOICE_REPORTING"]["ENABLE"]:
            send_invoice_report.delay()
            send_monthly_invoicing_reports_about_customers.delay()

        if settings.WALDUR_INVOICES["SEND_CUSTOMER_INVOICES"]:
            send_new_invoices_notification.delay()


@shared_task(name="invoices.send_invoice_notification")
def send_invoice_notification(invoice_uuid):
    """Sends email notification with invoice link to customer owners"""
    invoice = models.Invoice.objects.get(uuid=invoice_uuid)

    context = {
        "month": invoice.month,
        "year": invoice.year,
        "customer": invoice.customer.name,
        "link": core_utils.format_homeport_link("invoice/{uuid}", uuid=invoice_uuid),
    }

    emails = invoice.customer.get_owner_mails()

    filename = "{}_{}_{}.html".format(
        config.SITE_NAME.replace(" ", "_"),
        invoice.year,
        invoice.month,
    )
    attachment = utils.create_invoice_html(invoice)
    content_type = "text/html"

    logger.info(f"About to send invoice {invoice} notification to {emails}")
    core_utils.broadcast_mail(
        "invoices",
        "notification",
        context,
        emails,
        filename=filename,
        attachment=attachment,
        content_type=content_type,
    )


@shared_task(name="invoices.send_invoice_report")
def send_invoice_report(
    year=None, month=None, emails=None, include_settings_email=True
):
    """Sends aggregate accounting data as CSV"""
    if year and month:
        date = datetime.date(year, month, 1)
    else:
        date = get_previous_month()

    subject = render_to_string(
        "invoices/report_subject.txt",
        {
            "month": date.month,
            "year": date.year,
        },
    ).strip()
    body = render_to_string(
        "invoices/report_body.txt",
        {
            "month": date.month,
            "year": date.year,
        },
    ).strip()
    filename = "3M%02d%dWaldur.txt" % (date.month, date.year)
    invoices = models.Invoice.objects.filter(
        year=date.year, month=date.month, customer__archived=False
    )

    # Report should include only organizations that had accounting running during the invoice period.
    if settings.WALDUR_CORE["ENABLE_ACCOUNTING_START_DATE"]:
        invoices = invoices.filter(
            customer__accounting_start_date__lte=core_utils.month_end(date)
        )

    # Report should not include customers with 0 invoice items.
    invoices = [invoice for invoice in invoices if invoice.items.count() > 0]
    text_message = format_invoice_csv(invoices)

    # Please note that email body could be empty if there are no valid invoices
    recipient_emails = []
    if include_settings_email:
        recipient_emails.append(settings.WALDUR_INVOICES["INVOICE_REPORTING"]["EMAIL"])
    if emails:
        recipient_emails += emails
    logger.info(f"About to send accounting report to {recipient_emails}")
    core_utils.send_mail(
        subject=subject,
        body=body,
        to=recipient_emails,
        attachment=text_message,
        filename=filename,
    )


def format_invoice_csv(invoices):
    if not isinstance(invoices, list):
        invoices = [invoices]

    csv_params = settings.WALDUR_INVOICES["INVOICE_REPORTING"]["CSV_PARAMS"]

    if settings.WALDUR_INVOICES["INVOICE_REPORTING"].get("USE_SAF"):
        fields = serializers.SAFReportSerializer.Meta.fields
        stream = StringIO()
        writer = DictWriter(stream, fieldnames=fields, **csv_params)
        writer.writeheader()

        for invoice in invoices:
            items = utils.filter_invoice_items(
                invoice.items.order_by("project_name", "name")
            )
            serializer = serializers.SAFReportSerializer(items, many=True)
            writer.writerows(serializer.data)
        return stream.getvalue()
    elif settings.WALDUR_INVOICES["INVOICE_REPORTING"].get("USE_SAP"):
        fields = serializers.SAPReportSerializer.Meta.fields
        stream = StringIO()
        writer = DictWriter(stream, fieldnames=fields, **csv_params)
        writer.writeheader()

        for invoice in invoices:
            items = utils.filter_invoice_items(
                invoice.items.order_by("project_name", "name")
            )
            serializer = serializers.SAPReportSerializer(items, many=True)
            writer.writerows(serializer.data)
        return stream.getvalue()

    fields = serializers.InvoiceItemReportSerializer.Meta.fields
    stream = StringIO()
    writer = DictWriter(stream, fieldnames=fields, **csv_params)
    writer.writeheader()

    for invoice in invoices:
        items = utils.filter_invoice_items(invoice.items.all())
        serializer = serializers.InvoiceItemReportSerializer(items, many=True)
        writer.writerows(serializer.data)

    return stream.getvalue()


@shared_task(name="invoices.update_invoices_total_cost")
def update_invoices_total_cost():
    """Update cached total cost for current month invoices."""
    year = utils.get_current_year()
    month = utils.get_current_month()

    for invoice in models.Invoice.objects.filter(year=year, month=month):
        invoice.update_cache()


@shared_task
def send_new_invoices_notification():
    date = timezone.now()

    # invoice notifications are not sent if customer has a fixed price payment profile
    fixed_price_profiles = models.PaymentProfile.objects.filter(
        is_active=True, payment_type=models.PaymentType.FIXED_PRICE
    ).values_list("organization_id", flat=True)

    for invoice in (
        models.Invoice.objects.filter(year=date.year, month=date.month)
        .exclude(customer_id__in=fixed_price_profiles)
        .exclude(customer__archived=True)
    ):
        send_invoice_notification.delay(invoice.uuid.hex)


@shared_task(name="invoices.send_notifications_about_upcoming_ends")
def send_notifications_about_upcoming_ends():
    """Send notifications about upcoming end dates of fixed payment profiles."""
    upcoming_ends = utils.get_upcoming_ends_of_fixed_payment_profiles()

    for profile in upcoming_ends:
        context = {
            "organization_name": profile.organization.name,
            "end": utils.get_end_date_for_profile(profile),
            "contract_number": profile.attributes.get("contract_number", ""),
        }
        emails = profile.organization.get_owner_mails()
        core_utils.broadcast_mail(
            "invoices",
            "upcoming_ends_notification",
            context,
            emails,
        )


@shared_task(name="invoices.send_monthly_invoicing_reports_about_customers")
def send_monthly_invoicing_reports_about_customers():
    """Send monthly invoicing reports via email to configured recipients."""
    if settings.WALDUR_INVOICES["INVOICE_REPORTING"]["ENABLE"]:
        report = utils.get_monthly_invoicing_reports()
        today = timezone.datetime.today()
        filename = "%02d_%04d_invoice_report.html" % (today.month, today.year)
        subject = "Financial report for %02d-%04d" % (
            today.month,
            today.year,
        )
        body = "Financial report for %02d-%04d is attached." % (
            today.month,
            today.year,
        )
        emails = [settings.WALDUR_INVOICES["INVOICE_REPORTING"]["EMAIL"]]
        logger.info(f"About to send monthly invoicing report to {emails}")
        core_utils.send_mail(
            subject=subject,
            body=body,
            to=emails,
            attachment=report,
            filename=filename,
            content_type="text/html",
        )


def set_to_zero_overdue_credits(effective_date=None):
    set_current_user(core_utils.get_system_robot())
    today = timezone.localtime(timezone.now()).date()
    if effective_date is None:
        effective_date = today
    # Reject future effective_date: filtering by end_date < effective_date with a
    # date in the future would zero out credits whose end_date has not actually
    # arrived yet. Manual invocations with an off-by-many date have caused this
    # in production.
    if effective_date > today:
        raise ValueError(
            f"set_to_zero_overdue_credits refuses to run with a future "
            f"effective_date={effective_date} (today={today}). "
            f"This would zero credits whose end_date has not arrived yet."
        )
    with (
        transaction.atomic(),
        skip_credit_audit(),
        ledger.credit_transaction_type(
            models.CreditTransaction.Types.EXPIRY,
            # The month the balance was forfeited in. Expiry answers to no
            # invoice, so unlike a compensation there is no billed month to
            # borrow — but leaving it open drops forfeiture out of any
            # per-month total, which is where "Lost" is read. The month the
            # movement happened is the honest answer, and the only one the
            # evidence supports.
            billing_period=effective_date.replace(day=1),
        ),
    ):
        for credit in (
            models.CustomerCredit.objects.select_for_update()
            .filter(end_date__lt=effective_date)
            .exclude(value=0)
        ):
            # A savepoint per credit so that one broken row does not abort
            # zeroing of the remaining credits or the calling invoice task.
            try:
                with transaction.atomic():
                    old_value = int(credit.value)
                    credit.value = 0
                    credit.save(update_fields=["value"])
                    event_logger.emit(
                        "Credit has been set to zero due as the end date {credit_end_date} has arrived.",
                        event_type=EventType.SET_TO_ZERO_OVERDUE_CREDIT,
                        event_context={
                            "customer": credit.customer,
                            "credit_end_date": credit.end_date,
                            "old_value": old_value,
                            "new_value": 0,
                        },
                    )
            except Exception:
                logger.exception(
                    "Unable to set overdue customer credit %s to zero", credit.uuid
                )
                continue
        for project_credit in (
            models.ProjectCredit.objects.select_for_update()
            .filter(end_date__lt=effective_date)
            .exclude(value=0)
        ):
            try:
                with transaction.atomic():
                    old_value = int(project_credit.value)
                    project_credit.value = 0
                    project_credit.save(update_fields=["value"])
                    event_logger.emit(
                        "Project credit has been set to zero as the end date {credit_end_date} has arrived.",
                        event_type=EventType.SET_TO_ZERO_OVERDUE_CREDIT,
                        event_context={
                            "customer": project_credit.project.customer,
                            "project": project_credit.project,
                            "credit_end_date": project_credit.end_date,
                            "old_value": old_value,
                            "new_value": 0,
                        },
                    )
            except Exception:
                logger.exception(
                    "Unable to set overdue project credit %s to zero",
                    project_credit.uuid,
                )
                continue


def process_invoice_credits(invoice: models.Invoice):
    """Process credits for a given invoice.

    Uses select_for_update() to lock credit rows before reading,
    preventing lost updates when concurrent workers process invoices
    for the same customer. See WAL-9806.
    """
    with transaction.atomic():
        # Lock the customer credit row to prevent concurrent modifications.
        # The MonthlyCompensation will re-read the credit, but the lock
        # ensures no other transaction can modify it until we commit.
        models.CustomerCredit.objects.select_for_update().filter(
            customer=invoice.customer
        ).exists()
        # Also lock project credits that might be consumed.
        models.ProjectCredit.objects.select_for_update().filter(
            project__customer=invoice.customer
        ).exists()

        monthly_compensation = compensations.MonthlyCompensation(
            invoice.customer, invoice=invoice
        )
        monthly_compensation.apply_compensations()
        monthly_compensation.update_linear_expected_consumption()
