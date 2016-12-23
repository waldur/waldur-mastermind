import logging

from datetime import date

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from . import models


logger = logging.getLogger(__name__)


@shared_task(name='invoices.create_monthly_invoices_for_packages')
def create_monthly_invoices_for_packages():
    """
    This task performs following actions:
        - For every customer change state of the invoices for previous months from "pending" to "billed"
          and freeze their items.
        - Create new invoice for every customer in current month if not created yet.
    """
    today = date.today()

    old_invoices = models.Invoice.objects.filter(
        state=models.Invoice.States.PENDING,
        month__lt=today.month,
        year__lte=today.year,
    )
    for invoice in old_invoices:
        invoice.propagate(month=today.month, year=today.year)


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
