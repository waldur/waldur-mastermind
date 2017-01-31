from __future__ import unicode_literals

from django.utils import timezone

from . import models, log, registrators


def add_new_openstack_package_details_to_invoice(sender, instance, created=False, **kwargs):
    registrator = registrators.OpenStackItemRegistrator()

    if not created:
        return

    registrator.register(instance, timezone.now())


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    registrator = registrators.OpenStackItemRegistrator()
    registrator.terminate(instance)


def add_new_offering_details_to_invoice(sender, instance, created=False, **kwargs):
    registrator = registrators.OfferingItemRegistrator()
    # TODO [TM:1/31/17] check if state is OK

    if not created:
        return

    registrator.register(instance, timezone.now())


def update_invoice_on_offering_deletion(sender, instance, **kwargs):
    registrator = registrators.OfferingItemRegistrator()
    registrator.terminate(instance, timezone.now())


def log_invoice_state_transition(sender, instance, created=False, **kwargs):
    if created:
        return

    state = instance.state
    if state == models.Invoice.States.PENDING or state == instance.tracker.previous('state'):
        return

    if state == models.Invoice.States.CREATED:
        log.event_logger.invoice.info(
            'Invoice for customer {customer_name} has been created.',
            event_type='invoice_created',
            event_context={'month': instance.month, 'year': instance.year, 'customer': instance.customer}
        )
    elif state == models.Invoice.States.PAID:
        log.event_logger.invoice.info(
            'Invoice for customer {customer_name} has been paid.',
            event_type='invoice_paid',
            event_context={'month': instance.month, 'year': instance.year, 'customer': instance.customer}
        )
    elif state == models.Invoice.States.CANCELED:
        log.event_logger.invoice.info(
            'Invoice for customer {customer_name} has been canceled.',
            event_type='invoice_canceled',
            event_context={'month': instance.month, 'year': instance.year, 'customer': instance.customer}
        )


def set_tax_percent_on_invoice_creation(sender, instance, **kwargs):
    if instance.pk is not None:
        return

    payment_details = models.PaymentDetails.objects.filter(customer=instance.customer)
    if payment_details.exists():
        instance.tax_percent = payment_details.first().default_tax_percent
