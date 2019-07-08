from django.utils import timezone

from waldur_mastermind.invoices import registrators


def add_new_vm_to_invoice(sender, vm, **kwargs):
    registrators.RegistrationManager.register(vm, timezone.now())


def terminate_invoice_when_vm_deleted(sender, instance, **kwargs):
    registrators.RegistrationManager.terminate(instance, timezone.now())


def create_invoice_item_when_vm_is_updated(sender, vm, **kwargs):
    registrators.RegistrationManager.terminate(vm, timezone.now())
    registrators.RegistrationManager.register(vm, timezone.now())
