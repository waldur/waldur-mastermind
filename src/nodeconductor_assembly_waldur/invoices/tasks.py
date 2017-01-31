import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from . import models, registrators


logger = logging.getLogger(__name__)


@shared_task(name='invoices.create_monthly_invoices')
def create_monthly_invoices():
    registrator = registrators.InvoiceRegistrator()
    registrator.update_invoices()


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
