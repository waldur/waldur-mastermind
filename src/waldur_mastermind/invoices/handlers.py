import datetime
import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import signals as cost_signals
from waldur_mastermind.marketplace import models as marketplace_models

from . import log, models, registrators

logger = logging.getLogger(__name__)


def log_invoice_state_transition(sender, instance, created=False, **kwargs):
    if created:
        return

    state = instance.state
    if state == models.Invoice.States.PENDING or state == instance.tracker.previous(
        'state'
    ):
        return

    if state == models.Invoice.States.CREATED:
        log.event_logger.invoice.info(
            'Invoice for customer {customer_name} has been created.',
            event_type='invoice_created',
            event_context={
                'month': instance.month,
                'year': instance.year,
                'customer': instance.customer,
            },
        )
    elif state == models.Invoice.States.PAID:
        log.event_logger.invoice.info(
            'Invoice for customer {customer_name} has been paid.',
            event_type='invoice_paid',
            event_context={
                'month': instance.month,
                'year': instance.year,
                'customer': instance.customer,
            },
        )
    elif state == models.Invoice.States.CANCELED:
        log.event_logger.invoice.info(
            'Invoice for customer {customer_name} has been canceled.',
            event_type='invoice_canceled',
            event_context={
                'month': instance.month,
                'year': instance.year,
                'customer': instance.customer,
            },
        )


def set_tax_percent_on_invoice_creation(sender, instance, **kwargs):
    if instance.pk is not None:
        return

    instance.tax_percent = instance.customer.default_tax_percent


def set_project_name_on_invoice_item_creation(
    sender, instance, created=False, **kwargs
):
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
    for item in models.InvoiceItem.objects.filter(query).only('pk'):
        item.project_name = project.name
        item.save(update_fields=['project_name'])


def emit_invoice_created_event(sender, instance, created=False, **kwargs):
    if created:
        return

    state = instance.state
    if state != models.Invoice.States.CREATED or state == instance.tracker.previous(
        'state'
    ):
        return

    cost_signals.invoice_created.send(
        sender=models.Invoice,
        invoice=instance,
        issuer_details=settings.WALDUR_INVOICES['ISSUER_DETAILS'],
    )


def prevent_deletion_of_customer_with_invoice(sender, instance, user, **kwargs):
    if user.is_staff:
        return
    PENDING = models.Invoice.States.PENDING
    for invoice in models.Invoice.objects.filter(customer=instance):
        if invoice.state != PENDING or invoice.price > 0:
            raise ValidationError(
                _('Can\'t delete organization with invoice %s.') % invoice
            )


def update_current_cost_when_invoice_item_is_updated(
    sender, instance, created=False, **kwargs
):
    invoice_item = instance
    if created or set(invoice_item.tracker.changed()) & {
        'start',
        'end',
        'quantity',
        'unit_price',
    }:
        transaction.on_commit(lambda: invoice_item.invoice.update_current_cost())


def update_current_cost_when_invoice_item_is_deleted(sender, instance, **kwargs):
    def update_invoice():
        try:
            instance.invoice.update_current_cost()
        except ObjectDoesNotExist:
            # It is okay to skip cache invalidation if invoice has been already removed
            pass

    transaction.on_commit(update_invoice)


def projects_customer_has_been_changed(
    sender, project, old_customer, new_customer, created=False, **kwargs
):
    try:
        today = timezone.now()
        date = core_utils.month_start(today)

        invoice = models.Invoice.objects.get(
            customer=old_customer,
            state=models.Invoice.States.PENDING,
            month=date.month,
            year=date.year,
        )
    except models.Invoice.DoesNotExist:
        return

    new_invoice, create = registrators.RegistrationManager.get_or_create_invoice(
        new_customer, date
    )

    if create:
        invoice.items.filter(project=project).delete()
    else:
        invoice.items.filter(project=project).update(invoice=new_invoice)


def create_recurring_usage_if_invoice_has_been_created(
    sender, instance, created=False, **kwargs
):
    if not created:
        return

    invoice = instance

    now = timezone.now()
    prev_month = (now.replace(day=1) - datetime.timedelta(days=1)).date()
    prev_month_start = prev_month.replace(day=1)
    usages = marketplace_models.ComponentUsage.objects.filter(
        resource__project__customer=invoice.customer,
        recurring=True,
        billing_period__gte=prev_month_start,
    ).exclude(resource__state=marketplace_models.Resource.States.TERMINATED)

    if not usages:
        return

    for usage in usages:
        marketplace_models.ComponentUsage.objects.create(
            resource=usage.resource,
            component=usage.component,
            usage=usage.usage,
            description=usage.description,
            date=now,
            plan_period=usage.plan_period,
            recurring=usage.recurring,
            billing_period=core_utils.month_start(now),
        )
