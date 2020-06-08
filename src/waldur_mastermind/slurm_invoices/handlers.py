from django.utils import timezone

from waldur_mastermind.invoices import registrators

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

    if created:
        registrators.RegistrationManager.register(allocation)
        return

    invoice_items = registrators.RegistrationManager.get_item(allocation)

    package = utils.get_package(allocation)
    if package:
        for invoice_item in invoice_items:
            item_type = invoice_item.details['type']
            invoice_item.unit_price = utils.get_unit_deposit_usage(
                allocation_usage, package, item_type
            )
            invoice_item.quantity = getattr(allocation_usage, item_type + '_usage')
            invoice_item.save(update_fields=['unit_price', 'quantity'])


def update_allocation_deposit(sender, instance, created=False, **kwargs):
    allocation = instance

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
