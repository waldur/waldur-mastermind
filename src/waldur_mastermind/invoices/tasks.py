import base64
import logging
from csv import DictWriter
from io import StringIO

import pdfkit
from celery import chain, shared_task
from django.conf import settings
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices.utils import get_previous_month

from . import models, registrators, serializers, utils

logger = logging.getLogger(__name__)


@shared_task(name='invoices.create_monthly_invoices')
def create_monthly_invoices():
    """
    - For every customer change state of the invoices for previous months from "pending" to "billed"
      and freeze their items.
    - Create new invoice for every customer in current month if not created yet.
    """
    date = timezone.now()

    old_invoices = models.Invoice.objects.filter(
        Q(state=models.Invoice.States.PENDING, year__lt=date.year)
        | Q(state=models.Invoice.States.PENDING, year=date.year, month__lt=date.month)
    )
    for invoice in old_invoices:
        invoice.set_created()

    customers = structure_models.Customer.objects.all()
    if settings.WALDUR_CORE['ENABLE_ACCOUNTING_START_DATE']:
        customers = customers.filter(accounting_start_date__lt=timezone.now())

    for customer in customers.iterator():
        registrators.RegistrationManager.get_or_create_invoice(
            customer, core_utils.month_start(date)
        )

    if settings.WALDUR_INVOICES['INVOICE_REPORTING']['ENABLE']:
        send_invoice_report.delay()

    if settings.WALDUR_INVOICES['SEND_CUSTOMER_INVOICES']:
        chain(create_pdf_for_new_invoices.si(), send_new_invoices_notification.si())()
    else:
        create_pdf_for_new_invoices.delay()


@shared_task(name='invoices.send_invoice_notification')
def send_invoice_notification(invoice_uuid):
    """ Sends email notification with invoice link to customer owners """
    invoice = models.Invoice.objects.get(uuid=invoice_uuid)
    link_template = settings.WALDUR_INVOICES['INVOICE_LINK_TEMPLATE']

    if not link_template:
        logger.error(
            'INVOICE_LINK_TEMPLATE is not set. '
            'Sending of invoice notification is not available.'
        )
        return

    if '{uuid}' not in link_template:
        logger.error(
            'INVOICE_LINK_TEMPLATE must include \'{uuid}\' parameter. '
            'Sending of invoice notification is not available.'
        )
        return

    context = {
        'month': invoice.month,
        'year': invoice.year,
        'customer': invoice.customer.name,
        'link': link_template.format(uuid=invoice_uuid),
    }

    emails = [owner.email for owner in invoice.customer.get_owners()]
    filename = None
    attachment = None
    content_type = None

    if invoice._file:
        filename = '%s_%s_%s.pdf' % (
            settings.WALDUR_CORE['SITE_NAME'].replace(' ', '_'),
            invoice.year,
            invoice.month,
        )
        attachment = base64.b64decode(invoice._file)
        content_type = 'application/pdf'

    logger.debug(
        'About to send invoice {invoice} notification to {emails}'.format(
            invoice=invoice, emails=emails
        )
    )
    core_utils.broadcast_mail(
        'invoices',
        'notification',
        context,
        emails,
        filename=filename,
        attachment=attachment,
        content_type=content_type,
    )


@shared_task(name='invoices.send_invoice_report')
def send_invoice_report():
    """ Sends aggregate accounting data as CSV """
    date = get_previous_month()
    subject = render_to_string(
        'invoices/report_subject.txt', {'month': date.month, 'year': date.year,}
    ).strip()
    body = render_to_string(
        'invoices/report_body.txt', {'month': date.month, 'year': date.year,}
    ).strip()
    filename = '3M%02d%dWaldur.txt' % (date.month, date.year)
    invoices = models.Invoice.objects.filter(year=date.year, month=date.month)

    # Report should include only organizations that had accounting running during the invoice period.
    if settings.WALDUR_CORE['ENABLE_ACCOUNTING_START_DATE']:
        invoices = invoices.filter(
            customer__accounting_start_date__lte=core_utils.month_end(date)
        )

    # Report should not include customers with 0 invoice items.
    invoices = [invoice for invoice in invoices if invoice.items.count() > 0]
    text_message = format_invoice_csv(invoices)

    # Please note that email body could be empty if there are no valid invoices
    emails = [settings.WALDUR_INVOICES['INVOICE_REPORTING']['EMAIL']]
    logger.debug('About to send accounting report to {emails}'.format(emails=emails))
    core_utils.send_mail_with_attachment(
        subject=subject,
        body=body,
        to=emails,
        attachment=text_message,
        filename=filename,
    )


def format_invoice_csv(invoices):
    if not isinstance(invoices, list):
        invoices = [invoices]

    csv_params = settings.WALDUR_INVOICES['INVOICE_REPORTING']['CSV_PARAMS']

    if settings.WALDUR_INVOICES['INVOICE_REPORTING'].get('USE_SAF'):
        fields = serializers.SAFReportSerializer.Meta.fields
        stream = StringIO()
        writer = DictWriter(stream, fieldnames=fields, **csv_params)
        writer.writeheader()

        for invoice in invoices:
            items = utils.filter_invoice_items(invoice.items.all())
            serializer = serializers.SAFReportSerializer(items, many=True)
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


@shared_task(name='invoices.update_invoices_current_cost')
def update_invoices_current_cost():
    year = utils.get_current_year()
    month = utils.get_current_month()

    for invoice in models.Invoice.objects.filter(year=year, month=month):
        invoice.update_current_cost()


@shared_task
def create_invoice_pdf(serialized_invoice):
    invoice = core_utils.deserialize_instance(serialized_invoice)
    utils.create_invoice_pdf(invoice)


@shared_task
def create_pdf_for_all_invoices():
    for invoice in models.Invoice.objects.all():
        utils.create_invoice_pdf(invoice)


@shared_task
def create_pdf_for_new_invoices():
    date = timezone.now()
    for invoice in models.Invoice.objects.filter(year=date.year, month=date.month):
        utils.create_invoice_pdf(invoice)


@shared_task
def send_new_invoices_notification():
    date = timezone.now()

    # invoice notifications are not sent if customer has a fixed price payment profile
    fixed_price_profiles = models.PaymentProfile.objects.filter(
        is_active=True, payment_type=models.PaymentType.FIXED_PRICE
    ).values_list('organization_id', flat=True)

    for invoice in models.Invoice.objects.filter(
        year=date.year, month=date.month
    ).exclude(customer_id__in=fixed_price_profiles):
        send_invoice_notification.delay(invoice.uuid.hex)


@shared_task(name='invoices.send_notifications_about_upcoming_ends')
def send_notifications_about_upcoming_ends():
    upcoming_ends = utils.get_upcoming_ends_of_fixed_payment_profiles()

    for profile in upcoming_ends:
        context = {
            'organization_name': profile.organization.name,
            'end': utils.get_end_date_for_profile(profile),
            'contract_number': profile.attributes.get('contract_number', ''),
        }
        emails = [owner.email for owner in profile.organization.get_owners()]
        core_utils.broadcast_mail(
            'invoices', 'upcoming_ends_notification', context, emails,
        )


@shared_task(name='invoices.send_monthly_invoicing_reports_about_customers')
def send_monthly_invoicing_reports_about_customers():
    if settings.WALDUR_INVOICES['INVOICE_REPORTING']['ENABLE']:
        report = utils.get_monthly_invoicing_reports()
        pdf = pdfkit.from_string(report, False)
        today = timezone.datetime.today()
        filename = '%02d_%04d_invoice_report.pdf' % (today.month, today.year)
        subject = 'Financial report for %02d-%04d' % (today.month, today.year,)
        body = 'Financial report for %02d-%04d is attached.' % (
            today.month,
            today.year,
        )
        emails = [settings.WALDUR_INVOICES['INVOICE_REPORTING']['EMAIL']]
        core_utils.send_mail_with_attachment(
            subject=subject,
            body=body,
            to=emails,
            attachment=pdf,
            filename=filename,
            content_type='application/pdf',
        )
