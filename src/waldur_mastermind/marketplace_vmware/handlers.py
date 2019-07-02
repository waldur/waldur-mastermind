from django.utils import timezone

from waldur_mastermind.invoices import registrators
from waldur_vmware import models as vmware_models
from waldur_mastermind.marketplace import models as marketplace_models


def add_new_vm_to_invoice(sender, instance, created=False, **kwargs):
    if created:
        return

    order_item = instance

    if order_item.tracker.has_changed('state') and \
            order_item.state == marketplace_models.OrderItem.States.DONE and \
            order_item.type == marketplace_models.OrderItem.Types.CREATE and \
            order_item.resource and \
            order_item.resource.scope and \
            isinstance(order_item.resource.scope, vmware_models.VirtualMachine):
        registrators.RegistrationManager.register(order_item.resource.scope, timezone.now())


def terminate_invoice_when_vm_deleted(sender, instance, **kwargs):
    registrators.RegistrationManager.terminate(instance, timezone.now())
