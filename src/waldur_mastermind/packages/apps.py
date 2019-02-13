from django.apps import AppConfig
from django.db.models import signals


class PackageConfig(AppConfig):
    name = 'waldur_mastermind.packages'
    verbose_name = 'VPC packages'

    def ready(self):
        OpenStackPackage = self.get_model('OpenStackPackage')

        from waldur_mastermind.invoices import registrators
        from . import handlers, registrators as openstack_registrator
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

        registrators.RegistrationManager.add_registrator(
            OpenStackPackage,
            openstack_registrator.OpenStackItemRegistrator
        )

        signals.post_save.connect(
            handlers.add_new_openstack_package_details_to_invoice,
            sender=OpenStackPackage,
            dispatch_uid='waldur_mastermind.invoices.add_new_openstack_package_details_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.update_invoice_on_openstack_package_deletion,
            sender=OpenStackPackage,
            dispatch_uid='waldur_mastermind.invoices.update_invoice_on_openstack_package_deletion',
        )
