from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.slurm_invoices import registrators as slurm_registrators

from . import utils


def add_new_allocation_to_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    registrators.RegistrationManager.register(instance, timezone.now())


def terminate_invoice_when_allocation_deleted(sender, instance, **kwargs):
    registrators.RegistrationManager.terminate(instance, timezone.now())


def update_invoice_item_on_allocation_usage_update(
    sender, instance, created=False, **kwargs
):
    allocation_usage = instance
    allocation = allocation_usage.allocation

    package = utils.get_package(allocation)
    if package:
        start = timezone.now()
        end = core_utils.month_end(start)
        registrator = slurm_registrators.AllocationRegistrator()
        customer = registrator.get_customer(allocation)
        invoice = invoice_models.Invoice.objects.get(
            customer=customer, month=start.month, year=start.year,
        )

        registrator.create_or_update_items(
            allocation, allocation_usage, package, invoice, start, end
        )


def update_allocation_deposit(sender, instance, created=False, **kwargs):
    allocation = instance
    if allocation.batch_service != 'MOAB':
        return

    package = utils.get_package(allocation)
    if not package:
        return

    if created or allocation.usage_changed():
        if 'deposit_limit' in (kwargs.get('update_fields') or {}):
            return

        allocation.deposit_limit = utils.get_deposit_limit(allocation, package)
        if created:
            allocation.save()
        else:
            allocation.save(update_fields=['deposit_limit'])
