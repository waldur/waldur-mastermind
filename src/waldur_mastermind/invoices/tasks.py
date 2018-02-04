from __future__ import unicode_literals

import cStringIO
import datetime
import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.csv import UnicodeDictWriter
from waldur_core.structure import models as structure_models

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
        Q(state=models.Invoice.States.PENDING, year__lt=date.year) |
        Q(state=models.Invoice.States.PENDING, year=date.year, month__lt=date.month)
    )
    for invoice in old_invoices:
        invoice.set_created()
        invoice.freeze()

    customers = structure_models.Customer.objects.all()
    if settings.WALDUR_INVOICES['ENABLE_ACCOUNTING_START_DATE']:
        customers = customers.filter(accounting_start_date__lt=timezone.now())

    for customer in customers.iterator():
        registrators.RegistrationManager.get_or_create_invoice(customer, core_utils.month_start(date))

    if settings.WALDUR_INVOICES['INVOICE_REPORTING']['ENABLE']:
        send_invoice_report.delay()


@shared_task(name='invoices.send_invoice_notification')
def send_invoice_notification(invoice_uuid, link_template):
    """ Sends email notification with invoice link to customer owners """
    invoice = models.Invoice.objects.get(uuid=invoice_uuid)

    context = {
        'month': invoice.month,
        'year': invoice.year,
        'customer': invoice.customer.name,
        'link': link_template.format(uuid=invoice_uuid)
    }

    subject = render_to_string('invoices/notification_subject.txt', context).strip()
    text_message = render_to_string('invoices/notification_message.txt', context)
    html_message = render_to_string('invoices/notification_message.html', context)

    emails = [owner.email for owner in invoice.customer.get_owners()]

    logger.debug('About to send invoice {invoice} notification to {emails}'.format(invoice=invoice, emails=emails))
    send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, emails, html_message=html_message)


def get_previous_month():
    date = timezone.now()
    month, year = (date.month - 1, date.year) if date.month != 1 else (12, date.year - 1)
    return datetime.date(year, month, 1)


@shared_task(name='invoices.send_invoice_report')
def send_invoice_report():
    """ Sends aggregate accounting data as CSV """
    date = get_previous_month()
    subject = render_to_string('invoices/report_subject.txt', {
        'month': date.month,
        'year': date.year,
    }).strip()
    body = render_to_string('invoices/report_body.txt', {
        'month': date.month,
        'year': date.year,
    }).strip()
    filename = '3M%02d%dWaldur.txt' % (date.month, date.year)
    invoices = models.Invoice.objects.filter(year=date.year, month=date.month)

    # Report should include only organizations that had accounting running during the invoice period.
    if settings.WALDUR_INVOICES['ENABLE_ACCOUNTING_START_DATE']:
        invoices = invoices.filter(customer__accounting_start_date__lte=date)

    # Report should not include customers with 0 invoice sum.
    invoices = [invoice for invoice in invoices if invoice.total > 0]
    text_message = format_invoice_csv(invoices)

    # Please note that email body could be empty if there are no valid invoices
    emails = [settings.WALDUR_INVOICES['INVOICE_REPORTING']['EMAIL']]
    logger.debug('About to send accounting report to {emails}'.format(emails=emails))
    utils.send_mail_attachment(
        subject=subject,
        body=body,
        to=emails,
        attach_text=text_message,
        filename=filename
    )


def format_invoice_csv(invoices):
    if not isinstance(invoices, list):
        invoices = [invoices]

    csv_params = settings.WALDUR_INVOICES['INVOICE_REPORTING']['CSV_PARAMS']

    if settings.WALDUR_INVOICES['INVOICE_REPORTING'].get('USE_SAF'):
        fields = serializers.SAFReportSerializer.Meta.fields
        stream = cStringIO.StringIO()
        writer = UnicodeDictWriter(stream, fieldnames=fields, **csv_params)
        writer.writeheader()

        for invoice in invoices:
            serializer = serializers.SAFReportSerializer(invoice.items, many=True)
            writer.writerows(serializer.data)
        return stream.getvalue().decode('utf-8')

    fields = serializers.InvoiceItemReportSerializer.Meta.fields
    stream = cStringIO.StringIO()
    writer = UnicodeDictWriter(stream, fieldnames=fields, **csv_params)
    writer.writeheader()

    for invoice in invoices:
        openstack_items = invoice.openstack_items.all().select_related('invoice__customer')
        openstack_serializer = serializers.OpenStackItemReportSerializer(openstack_items, many=True)
        writer.writerows(openstack_serializer.data)

        offering_items = invoice.offering_items.all().select_related('invoice__customer')
        offering_serializer = serializers.OfferingItemReportSerializer(offering_items, many=True)
        writer.writerows(offering_serializer.data)

        generic_items = invoice.generic_items.all().select_related('invoice__customer')
        generic_serializer = serializers.GenericItemReportSerializer(generic_items, many=True)
        writer.writerows(generic_serializer.data)

    return stream.getvalue().decode('utf-8')
