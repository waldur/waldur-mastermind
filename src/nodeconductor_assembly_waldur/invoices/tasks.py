import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from nodeconductor.core import utils as core_utils
from nodeconductor.structure import models as structure_models

from . import models, registrators


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
        with transaction.atomic():
            invoice.set_created()
            for registrator in registrators.RegistrationManager.all_registrators:
                registrator.freeze_invoice(invoice)

    for customer in structure_models.Customer.objects.iterator():
        for registrator in registrators.RegistrationManager.all_registrators:
            items = registrator.get_chargeable_items(customer)
            if items:
                with transaction.atomic():
                    invoice, created = models.Invoice.objects.get_or_create(
                        customer=customer,
                        month=date.month,
                        year=date.year,
                    )
                    if created:
                        registrator.register_items(items, invoice=invoice, start=core_utils.month_start(date))


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
