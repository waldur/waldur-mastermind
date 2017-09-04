from __future__ import unicode_literals

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from nodeconductor.cost_tracking import signals as cost_signals
from nodeconductor_assembly_waldur.support import models as support_models

from . import tasks, models, log, registrators


def add_new_openstack_package_details_to_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    registrators.RegistrationManager.register(instance, timezone.now())


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    registrators.RegistrationManager.terminate(instance, timezone.now())


def update_invoice_on_offering_deletion(sender, instance, **kwargs):
    state = instance.state
    if state == support_models.Offering.States.TERMINATED:
        # no need to terminate offering item if it was already terminated before.
        return

    registrators.RegistrationManager.terminate(instance, timezone.now())


def add_new_offering_details_to_invoice(sender, instance, created=False, **kwargs):
    state = instance.state
    if (state == support_models.Offering.States.OK
        and support_models.Offering.States.REQUESTED == instance.tracker.previous('state')):
        registrators.RegistrationManager.register(instance, timezone.now())
    if (state == support_models.Offering.States.TERMINATED
        and support_models.Offering.States.OK == instance.tracker.previous('state')):
        registrators.RegistrationManager.terminate(instance, timezone.now())


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


def set_project_name_on_invoice_item_creation(sender, instance, created=False, **kwargs):
    if created:
        item = instance
        item.project_name = item.project.name
        item.project_uuid = item.project.uuid.hex
        item.save(update_fields=('project_name', 'project_uuid'))


def update_invoice_item_on_project_name_update(sender, instance, **kwargs):
    project = instance

    if not project.tracker.has_changed('name'):
        return

    query = Q(project=project, invoice__state=models.Invoice.States.PENDING)
    for model in models.InvoiceItem.get_all_models():
        for item in model.objects.filter(query).only('pk'):
            item.project_name = project.name
            item.save(update_fields=['project_name'])


def send_invoice_report(sender, instance, created=False, **kwargs):
    if not settings.INVOICES['INVOICE_REPORTING']['ENABLE']:
        return
    invoice = instance
    if invoice.tracker.has_changed('state') and invoice.state == models.Invoice.States.CREATED:
        tasks.send_invoice_report.delay(invoice.uuid.hex)


def emit_invoice_created_event(sender, instance, created=False, **kwargs):
    if created:
        return

    state = instance.state
    if state != models.Invoice.States.CREATED or state == instance.tracker.previous('state'):
        return

    if state == models.Invoice.States.CREATED:
        cost_signals.invoice_created.send(sender=models.Invoice,
                                          invoice=instance,
                                          issuer_details=settings.INVOICES['ISSUER_DETAILS'])
