from django.apps import AppConfig
from django.db.models import signals


class PackageConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.packages'
    verbose_name = 'VPC packages'

    def ready(self):
        OpenStackPackage = self.get_model('OpenStackPackage')

        from . import handlers

        signals.post_save.connect(
            handlers.log_openstack_package_creation,
            sender=OpenStackPackage,
            dispatch_uid='nodeconductor_assembly_waldur.packages.log_openstack_package_creation',
        )

        signals.pre_delete.connect(
            handlers.log_openstack_package_deletion,
            sender=OpenStackPackage,
            dispatch_uid='nodeconductor_assembly_waldur.packages.log_openstack_package_deletion',
        )
