from __future__ import unicode_literals

from django.utils import timezone

from nodeconductor_assembly_waldur.packages import models as package_models

from . import models, log


def add_new_openstack_package_details_to_invoice(sender, instance, created=False, **kwargs):
    package = instance
    if not created:
        return

    now = timezone.now()
    customer = package.tenant.service_project_link.project.customer
    invoice, created = models.Invoice.objects.get_or_create(
        customer=customer,
        month=now.month,
        year=now.year,
    )

    if created:
        packages_to_register = package_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer,
        ).distinct().iterator()
    else:
        packages_to_register = [package]

    for package in packages_to_register:
        invoice.register_package(package, start=now)


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    now = timezone.now()
    item = models.OpenStackItem.objects.get(
        package=instance,
        invoice__customer=instance.tenant.service_project_link.project.customer,
        invoice__state=models.Invoice.States.PENDING,
        invoice__year=now.year,
        invoice__month=now.month,
    )
    item.freeze(end=now, package_deletion=True)


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
