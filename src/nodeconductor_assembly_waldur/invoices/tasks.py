import cStringIO
import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from nodeconductor.core import utils as core_utils
from nodeconductor.core.csv import UnicodeDictWriter
from nodeconductor.structure import models as structure_models

from . import models, registrators, serializers


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
    if settings.INVOICES['ENABLE_ACCOUNTING_START_DATE']:
        customers = customers.filter(
            Q(payment_details__accounting_start_date__lt=timezone.now()) |
            Q(payment_details__isnull=True)
        )
    for customer in customers.iterator():
        registrators.RegistrationManager.get_or_create_invoice(customer, core_utils.month_start(date))


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

    subject = render_to_string('invoices/notification_subject.txt', context)
    text_message = render_to_string('invoices/notification_message.txt', context)
    html_message = render_to_string('invoices/notification_message.html', context)

    emails = [owner.email for owner in invoice.customer.get_owners()]

    logger.debug('About to send invoice {invoice} notification to {emails}'.format(invoice=invoice, emails=emails))
    send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, emails, html_message=html_message)


@shared_task(name='invoices.send_invoice_report')
def send_invoice_report(invoice_uuid):
    """ Sends accounting data as CSV """
    invoice = models.Invoice.objects.get(uuid=invoice_uuid)

    context = {
        'month': invoice.month,
        'year': invoice.year,
        'customer': invoice.customer.name,
    }

    subject = render_to_string('invoices/report_subject.txt', context)
    text_message = format_invoice_csv(invoice)
    emails = [settings.INVOICES['INVOICE_REPORTING']['EMAIL']]

    logger.debug('About to send invoice {invoice} report to {emails}'.format(invoice=invoice, emails=emails))
    send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, emails)


def format_invoice_csv(invoice):
    csv_params = settings.INVOICES['INVOICE_REPORTING']['CSV_PARAMS']
    fields = serializers.InvoiceItemReportSerializer.Meta.fields
    stream = cStringIO.StringIO()
    writer = UnicodeDictWriter(stream, fieldnames=fields, **csv_params)
    writer.writeheader()

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
