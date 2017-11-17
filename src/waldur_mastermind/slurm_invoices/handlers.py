from django.utils import timezone

from waldur_mastermind.invoices import registrators


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

    has_changed = (allocation.tracker.has_changed('cpu_usage') or
                   allocation.tracker.has_changed('gpu_usage') or
                   allocation.tracker.has_changed('ram_usage'))
    if not has_changed:
        return

    invoice_item = registrators.RegistrationManager.get_item(allocation)
    if not invoice_item:
        return
    registrator = registrators.RegistrationManager.get_registrator(allocation)
    package = registrator.get_package(allocation)
    if package:
        invoice_item.unit_price = registrator.get_price(allocation, package)
        invoice_item.save(update_fields=['unit_price'])
