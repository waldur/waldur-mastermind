from django.apps import AppConfig

from django.db.models import signals


class PackageConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.packages'
    verbose_name = 'Waldur assembly Packages'

    def ready(self):
        from . import handlers, models

        signals.post_save.connect(
            handlers.add_new_openstack_package_details_to_invoice,
            sender=models.OpenStackPackage,
            dispatch_uid='nodeconductor_assembly_waldur.packages.add_new_openstack_package_details_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.update_invoice_on_openstack_package_deletion,
            sender=models.OpenStackPackage,
            dispatch_uid='nodeconductor_assembly_waldur.packages.update_invoice_on_openstack_package_deletion',
        )
