import logging

from django.conf import settings
from django.db import transaction
from waldur_core.logging import models as logging_models

from .log import event_logger
from . import models, helpers


logger = logging.getLogger(__name__)


def log_invoice_save(sender, instance, created=False, **kwargs):
    if created:
        event_logger.paypal_invoice.info(
            '{invoice_invoice_date}-{invoice_end_date}. Invoice for customer {customer_name} has been created.',
            event_type='invoice_creation_succeeded',
            event_context={
                'invoice': instance,
            })
    else:
        event_logger.paypal_invoice.info(
            '{invoice_invoice_date}-{invoice_end_date}. Invoice for customer {customer_name} has been updated.',
            event_type='invoice_update_succeeded',
            event_context={
                'invoice': instance,
            })


def log_invoice_delete(sender, instance, **kwargs):
    event_logger.paypal_invoice.info(
        '{invoice_invoice_date}-{invoice_end_date}. Invoice for customer {customer_name} has been deleted.',
        event_type='invoice_deletion_succeeded',
        event_context={
            'invoice': instance,
        })


def add_email_hooks_to_user(sender, instance, created, **kwargs):
    if not settings.WALDUR_PAYPAL['ENABLED']:
        return

    if not created or not instance.tracker.has_changed('email') or not instance.email:
        return

    event_types = ['invoice_creation_succeeded', 'payment_creation_succeeded',
                   'payment_approval_succeeded', 'payment_cancel_succeeded']
    user = instance

    with transaction.atomic():
        logging_models.EmailHook.objects.update_or_create(
            user=user,
            defaults={
                'event_types': event_types,
                'email': user.email
            }
        )


def create_invoice(sender, invoice, issuer_details, **kwargs):
    """
    Creates an invoice when customer is "billed".
    :param sender: Invoice model
    :param invoice: Invoice instance
    :param issuer_details: details about issuer
    """
    if not invoice.items:
        return

    price = sum([item.price for item in invoice.items])

    if not price:
        return

    paypal_invoice = models.Invoice(
        customer=invoice.customer,
        year=invoice.year,
        month=invoice.month,
        invoice_date=invoice.invoice_date,
        end_date=invoice.due_date,
        tax_percent=invoice.tax_percent,
        issuer_details=issuer_details)

    paypal_invoice.payment_details = {
        'name': invoice.customer.name,
        'address': invoice.customer.address,
        'country': invoice.customer.country,
        'country_name': invoice.customer.get_country_display(),
        'email': invoice.customer.email,
        'postal': invoice.customer.postal,
        'phone_number': invoice.customer.phone_number,
        'bank_name': invoice.customer.bank_name,
        'bank_account': invoice.customer.bank_account,
    }

    paypal_invoice.save()

    for item in invoice.items:
        models.InvoiceItem.objects.create(
            invoice=paypal_invoice,
            price=item.price,
            tax=item.tax,
            quantity=helpers.get_invoice_item_quantity(item),
            unit_price=item.unit_price,
            unit_of_measure=helpers.convert_unit_of_measure(item.unit),
            name=item.name,
            start=item.start,
            end=item.end)
