import logging

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_vmware import CPU_TYPE, RAM_TYPE, DISK_TYPE

logger = logging.getLogger(__name__)


def add_new_vm_to_invoice(sender, vm, **kwargs):
    registrators.RegistrationManager.register(vm, timezone.now())


def terminate_invoice_when_vm_deleted(sender, instance, **kwargs):
    registrators.RegistrationManager.terminate(instance, timezone.now())


def create_invoice_item_when_vm_is_updated(sender, vm, **kwargs):
    registrators.RegistrationManager.terminate(vm, timezone.now())
    registrators.RegistrationManager.register(vm, timezone.now())


def update_marketplace_resource_limits_when_vm_is_updated(sender, vm, **kwargs):
    try:
        resource = models.Resource.objects.get(scope=vm)
    except ObjectDoesNotExist:
        logger.debug('Skipping marketplace resource update for vm '
                     'because marketplace resource does not exist. '
                     'Resource ID: %s', core_utils.serialize_instance(vm))
    else:
        resource.limits[CPU_TYPE] = vm.cores
        resource.limits[RAM_TYPE] = vm.ram
        resource.limits[DISK_TYPE] = vm.total_disk
        resource.save()
