import logging

from datetime import date

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string

from nodeconductor.core import utils as core_utils
from nodeconductor.structure import models as structure_models
from nodeconductor_assembly_waldur.packages import models as package_models

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
        Q(state=models.Invoice.States.PENDING, year__lt=today.year) |
        Q(state=models.Invoice.States.PENDING, year=today.year, month__lt=today.month)
    )
    for invoice in old_invoices:
        invoice.set_created()

    for customer in structure_models.Customer.objects.iterator():
        packages_query = package_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer).distinct()

        if not packages_query.exists():
            continue

        with transaction.atomic():
            invoice, created = models.Invoice.objects.get_or_create(
                customer=customer,
                month=today.month,
                year=today.year,
            )

            if created:
                for package in packages_query.iterator():
                    invoice.register_package(package, start=core_utils.month_start(today))


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
