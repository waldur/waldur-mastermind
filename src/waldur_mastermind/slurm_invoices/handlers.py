from django.utils import timezone

from waldur_mastermind.invoices import registrators

from . import utils


def add_new_allocation_to_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    registrators.RegistrationManager.register(instance, timezone.now())


def terminate_invoice_when_allocation_deleted(sender, instance, **kwargs):
    registrators.RegistrationManager.terminate(instance, timezone.now())


def terminate_invoice_when_allocation_cancelled(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('is_active') and not instance.is_active:
        registrators.RegistrationManager.terminate(instance, timezone.now())


def update_invoice_item_on_allocation_usage_update(sender, instance, created=False, **kwargs):
    if created:
        return

    allocation = instance
    if not allocation.usage_changed():
        return

    invoice_item = registrators.RegistrationManager.get_item(allocation)
    if not invoice_item:
        return

    package = utils.get_package(allocation)
    if package:
        invoice_item.unit_price = utils.get_deposit_usage(allocation, package)
        invoice_item.save(update_fields=['unit_price'])


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
