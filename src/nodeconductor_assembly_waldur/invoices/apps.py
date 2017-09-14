from django.apps import AppConfig
from django.db.models import signals


class InvoiceConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.invoices'
    verbose_name = 'Invoices'

    def ready(self):
        from nodeconductor.structure import models as structure_models
        from nodeconductor_assembly_waldur.invoices.plugins import offering_registrator
        from nodeconductor_assembly_waldur.invoices.plugins import openstack_registrator
        from nodeconductor_assembly_waldur.packages import models as packages_models
        from nodeconductor_assembly_waldur.support import models as support_models

        from . import handlers, models, registrators

        registrators.RegistrationManager.add_registrator(
            support_models.Offering,
            offering_registrator.OfferingItemRegistrator
        )

        registrators.RegistrationManager.add_registrator(
            packages_models.OpenStackPackage,
            openstack_registrator.OpenStackItemRegistrator
        )

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

        signals.post_save.connect(
            handlers.emit_invoice_created_event,
            sender=models.Invoice,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.emit_invoice_created_event',
        )

        for index, model in enumerate(models.InvoiceItem.get_all_models()):
            signals.post_save.connect(
                handlers.set_project_name_on_invoice_item_creation,
                sender=model,
                dispatch_uid='nodeconductor_assembly_waldur.invoices.'
                             'set_project_name_on_invoice_item_creation_%s_%s' % (index, model.__class__),
            )

        signals.post_save.connect(
            handlers.update_invoice_item_on_project_name_update,
            sender=structure_models.Project,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.update_invoice_item_on_project_name_update',
        )

        signals.post_save.connect(
            handlers.send_invoice_report,
            sender=models.Invoice,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.send_invoice_report',
        )
