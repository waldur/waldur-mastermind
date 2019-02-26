from __future__ import unicode_literals

import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_core.cost_tracking import signals as cost_signals
from waldur_core.core import utils as core_utils

from . import models, log, tasks

logger = logging.getLogger(__name__)


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

    instance.tax_percent = instance.customer.default_tax_percent


def set_project_name_on_invoice_item_creation(sender, instance, created=False, **kwargs):
    if created and instance.project:
        item = instance
        item.project_name = item.project.name
        item.project_uuid = item.project.uuid.hex
        item.save(update_fields=('project_name', 'project_uuid'))


def update_invoice_item_on_project_name_update(sender, instance, **kwargs):
    project = instance

    if not project.tracker.has_changed('name'):
        return

    query = Q(project=project, invoice__state=models.Invoice.States.PENDING)
    for item in models.GenericInvoiceItem.objects.filter(query).only('pk'):
        item.project_name = project.name
        item.save(update_fields=['project_name'])


def emit_invoice_created_event(sender, instance, created=False, **kwargs):
    if created:
        return

    state = instance.state
    if state != models.Invoice.States.CREATED or state == instance.tracker.previous('state'):
        return

    cost_signals.invoice_created.send(sender=models.Invoice,
                                      invoice=instance,
                                      issuer_details=settings.WALDUR_INVOICES['ISSUER_DETAILS'])


def prevent_deletion_of_customer_with_invoice(sender, instance, user, **kwargs):
    if user.is_staff:
        return
    PENDING = models.Invoice.States.PENDING
    for invoice in models.Invoice.objects.filter(customer=instance):
        if invoice.state != PENDING or invoice.price > 0:
            raise ValidationError(_('Can\'t delete organization with invoice %s.') % invoice)


def update_current_cost_when_invoice_item_is_updated(sender, instance, created=False, **kwargs):
    invoice_item = instance
    if created or set(invoice_item.tracker.changed()) & {'start', 'end', 'quantity', 'unit_price'}:
        transaction.on_commit(lambda: invoice_item.invoice.update_current_cost())


def update_current_cost_when_invoice_item_is_deleted(sender, instance, **kwargs):
    def update_invoice():
        try:
            instance.invoice.update_current_cost()
        except ObjectDoesNotExist:
            # It is okay to skip cache invalidation if invoice has been already removed
            pass

    transaction.on_commit(update_invoice)


@transaction.atomic()
def adjust_openstack_items_for_downtime(downtime):
    items = models.GenericInvoiceItem.objects.filter(
        downtime.get_intersection_subquery(),
        scope=downtime.package,
    )

    for item in items:
        # outside
        if downtime.start <= item.start and item.end <= downtime.end:
            item.create_compensation(item.name, start=item.start, end=item.end)

        # inside
        elif item.start <= downtime.start and downtime.end <= item.end:
            item.create_compensation(item.name, start=downtime.start, end=downtime.end)

        # left
        elif downtime.end >= item.start and downtime.end <= item.end:
            item.create_compensation(item.name, start=item.start, end=downtime.end)

        # right
        elif downtime.start >= item.start and downtime.start <= item.end:
            item.create_compensation(item.name, start=downtime.start, end=item.end)


def adjust_invoice_items_for_downtime(sender, instance, created=False, **kwargs):
    downtime = instance
    if not created:
        logger.warning('Invoice items are not adjusted when downtime record is changed. '
                       'Record ID: %s', downtime.id)

    adjust_openstack_items_for_downtime(downtime)


def update_invoice_pdf(sender, instance, created=False, **kwargs):
    if created:
        return

    invoice = instance

    if not invoice.tracker.has_changed('current_cost'):
        return

    serialized_invoice = core_utils.serialize_instance(invoice)
    tasks.create_invoice_pdf.delay(serialized_invoice)
