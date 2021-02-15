from django.utils import timezone

from waldur_core.core import utils as core_utils
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
        invoice, _ = registrators.RegistrationManager.get_or_create_invoice(
            customer, core_utils.month_start(timezone.now())
        )

        registrator.create_or_update_items(
            allocation, allocation_usage, package, invoice, start, end
        )
