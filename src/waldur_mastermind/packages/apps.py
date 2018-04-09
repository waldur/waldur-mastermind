from django.apps import AppConfig
from django.db.models import signals


class PackageConfig(AppConfig):
    name = 'waldur_mastermind.packages'
    verbose_name = 'VPC packages'

    def ready(self):
        OpenStackPackage = self.get_model('OpenStackPackage')

        from . import handlers
        from . import cost_planning  # noqa: F401

        signals.post_save.connect(
            handlers.log_openstack_package_creation,
            sender=OpenStackPackage,
            dispatch_uid='waldur_mastermind.packages.log_openstack_package_creation',
        )

        signals.pre_delete.connect(
            handlers.log_openstack_package_deletion,
            sender=OpenStackPackage,
            dispatch_uid='waldur_mastermind.packages.log_openstack_package_deletion',
        )
