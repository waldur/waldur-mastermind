from django.apps import AppConfig
from django.db.models import signals


class InvoiceConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.invoices'
    verbose_name = 'Waldur assembly Invoices'

    def ready(self):
        from nodeconductor_assembly_waldur.support import models as support_models
        from nodeconductor_assembly_waldur.packages import models as packages_models

        from . import handlers, models

        signals.post_save.connect(
            handlers.add_new_openstack_package_details_to_invoice,
            sender=packages_models.OpenStackPackage,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.add_new_openstack_package_details_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.update_invoice_on_openstack_package_deletion,
            sender=packages_models.OpenStackPackage,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.update_invoice_on_openstack_package_deletion',
        )

        signals.post_save.connect(
            handlers.add_new_offering_details_to_invoice,
            sender=support_models.Offering,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.add_new_offering_details_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.update_invoice_on_offering_deletion,
            sender=support_models.Offering,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.update_invoice_on_offering_deletion',
        )

        signals.pre_save.connect(
            handlers.set_tax_percent_on_invoice_creation,
            sender=models.Invoice,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.set_tax_percent_on_invoice_creation',
        )

        signals.post_save.connect(
            handlers.log_invoice_state_transition,
            sender=models.Invoice,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.log_invoice_state_transition',
        )
