import logging

from django.core.exceptions import ObjectDoesNotExist

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_vmware import CPU_TYPE, DISK_TYPE, RAM_TYPE

logger = logging.getLogger(__name__)


def update_marketplace_resource_limits_when_vm_is_updated(sender, vm, **kwargs):
    try:
        resource = models.Resource.objects.get(scope=vm)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping marketplace resource update for vm "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            core_utils.serialize_instance(vm),
        )
    else:
        resource.limits[CPU_TYPE] = vm.cores
        resource.limits[RAM_TYPE] = vm.ram
        resource.limits[DISK_TYPE] = vm.total_disk
        resource.save()
