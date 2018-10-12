from django.apps import AppConfig
from django.db.models import signals


class InvoiceConfig(AppConfig):
    name = 'waldur_mastermind.invoices'
    verbose_name = 'Invoices'

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.structure import models as structure_models
        from waldur_mastermind.invoices.plugins import offering_registrator
        from waldur_mastermind.invoices.plugins import openstack_registrator
        from waldur_mastermind.packages import models as packages_models
        from waldur_mastermind.support import models as support_models

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
            dispatch_uid='waldur_mastermind.invoices.add_new_openstack_package_details_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.update_invoice_on_openstack_package_deletion,
            sender=packages_models.OpenStackPackage,
            dispatch_uid='waldur_mastermind.invoices.update_invoice_on_openstack_package_deletion',
        )

        signals.post_save.connect(
            handlers.add_new_offering_details_to_invoice,
            sender=support_models.Offering,
            dispatch_uid='waldur_mastermind.invoices.add_new_offering_details_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.update_invoice_on_offering_deletion,
            sender=support_models.Offering,
            dispatch_uid='waldur_mastermind.invoices.update_invoice_on_offering_deletion',
        )

        signals.pre_save.connect(
            handlers.set_tax_percent_on_invoice_creation,
            sender=models.Invoice,
            dispatch_uid='waldur_mastermind.invoices.set_tax_percent_on_invoice_creation',
        )

        signals.post_save.connect(
            handlers.log_invoice_state_transition,
            sender=models.Invoice,
            dispatch_uid='waldur_mastermind.invoices.log_invoice_state_transition',
        )

        signals.post_save.connect(
            handlers.emit_invoice_created_event,
            sender=models.Invoice,
            dispatch_uid='waldur_mastermind.invoices.emit_invoice_created_event',
        )

        for index, model in enumerate(models.InvoiceItem.get_all_models()):
            signals.post_save.connect(
                handlers.set_project_name_on_invoice_item_creation,
                sender=model,
                dispatch_uid='waldur_mastermind.invoices.'
                             'set_project_name_on_invoice_item_creation_%s_%s' % (index, model.__class__),
            )

            signals.post_save.connect(
                handlers.update_current_cost_when_invoice_item_is_updated,
                sender=model,
                dispatch_uid='waldur_mastermind.invoices.'
                             'update_current_cost_when_invoice_item_is_updated_%s_%s' %
                             (index, model.__class__),
            )

            signals.post_delete.connect(
                handlers.update_current_cost_when_invoice_item_is_deleted,
                sender=model,
                dispatch_uid='waldur_mastermind.invoices.'
                             'update_current_cost_when_invoice_item_is_deleted_%s_%s' %
                             (index, model.__class__),
            )

        signals.post_save.connect(
            handlers.update_invoice_item_on_project_name_update,
            sender=structure_models.Project,
            dispatch_uid='waldur_mastermind.invoices.update_invoice_item_on_project_name_update',
        )

        core_signals.pre_delete_validate.connect(
            handlers.prevent_deletion_of_customer_with_invoice,
            sender=structure_models.Customer,
            dispatch_uid='waldur_mastermind.invoices.prevent_deletion_of_customer_with_invoice',
        )

        signals.post_save.connect(
            handlers.adjust_invoice_items_for_downtime,
            sender=models.ServiceDowntime,
            dispatch_uid='waldur_mastermind.invoices.adjust_invoice_items_for_downtime',
        )
