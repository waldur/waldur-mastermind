from .models import Disk

"""
Handlers for disks.
"""


def update_vm_total_disk_when_disk_is_created_or_updated(
    sender, instance: Disk, created=False, **kwargs
):
    vm = instance.vm
    vm.disk = vm.total_disk
    vm.save(update_fields=["disk"])


def update_vm_total_disk_when_disk_is_deleted(sender, instance: Disk, **kwargs):
    vm = instance.vm
    vm.disk = vm.total_disk
    vm.save(update_fields=["disk"])
