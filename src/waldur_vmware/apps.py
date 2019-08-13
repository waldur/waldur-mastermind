from django.apps import AppConfig
from django.db.models import signals


class VMwareConfig(AppConfig):
    name = 'waldur_vmware'
    verbose_name = 'VMware'
    service_name = 'VMware'

    def ready(self):
        from waldur_core.structure import SupportedServices

        from .backend import VMwareBackend
        from . import handlers, models

        SupportedServices.register_backend(VMwareBackend)

        signals.post_save.connect(
            handlers.update_vm_total_disk_when_disk_is_created_or_updated,
            sender=models.Disk,
            dispatch_uid='waldur_vmware.handlers.'
                         'update_vm_total_disk_when_disk_is_created_or_updated',
        )

        signals.post_delete.connect(
            handlers.update_vm_total_disk_when_disk_is_deleted,
            sender=models.Disk,
            dispatch_uid='waldur_vmware.handlers.'
                         'update_vm_total_disk_when_disk_is_deleted',
        )
