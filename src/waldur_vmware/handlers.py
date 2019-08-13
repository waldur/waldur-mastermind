def update_vm_total_disk_when_disk_is_created_or_updated(sender, instance, created=False, **kwargs):
    vm = instance.vm
    vm.disk = vm.total_disk
    vm.save(update_fields=['disk'])


def update_vm_total_disk_when_disk_is_deleted(sender, instance, **kwargs):
    vm = instance.vm
    vm.disk = vm.total_disk
    vm.save(update_fields=['disk'])
