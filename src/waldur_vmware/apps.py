from celery.signals import worker_process_shutdown
from django.apps import AppConfig
from django.db.models import signals


class VMwareConfig(AppConfig):
    name = "waldur_vmware"
    verbose_name = "VMware"
    service_name = "VMware"

    def ready(self):
        from waldur_core.structure.registry import SupportedServices

        from . import handlers, models, sessions
        from .backend import VMwareBackend

        SupportedServices.register_backend(VMwareBackend)

        # vCenter sessions outlive the task that opened them, so a worker
        # process that stops between pulls would leave them for vCenter to
        # expire. The atexit hook in sessions covers an ordinary interpreter
        # exit; this covers a worker process being recycled while the parent
        # lives on.
        worker_process_shutdown.connect(
            sessions.close_all_sessions,
            dispatch_uid="waldur_vmware.sessions.close_all_sessions",
        )

        signals.post_save.connect(
            handlers.update_vm_total_disk_when_disk_is_created_or_updated,
            sender=models.Disk,
            dispatch_uid="waldur_vmware.handlers."
            "update_vm_total_disk_when_disk_is_created_or_updated",
        )

        signals.post_delete.connect(
            handlers.update_vm_total_disk_when_disk_is_deleted,
            sender=models.Disk,
            dispatch_uid="waldur_vmware.handlers."
            "update_vm_total_disk_when_disk_is_deleted",
        )
