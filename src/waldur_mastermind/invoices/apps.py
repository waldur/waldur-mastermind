from django.apps import AppConfig
from django.db.models import signals


class InvoiceConfig(AppConfig):
    name = 'waldur_mastermind.invoices'
    verbose_name = 'Invoices'

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.structure import models as structure_models

        from . import handlers, models

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

        signals.post_save.connect(
            handlers.update_invoice_pdf,
            sender=models.Invoice,
            dispatch_uid='waldur_mastermind.invoices.update_invoice_pdf',
        )

        signals.post_save.connect(
            handlers.set_project_name_on_invoice_item_creation,
            sender=models.GenericInvoiceItem,
            dispatch_uid='waldur_mastermind.invoices.set_project_name_on_invoice_item_creation',
        )

        signals.post_save.connect(
            handlers.update_current_cost_when_invoice_item_is_updated,
            sender=models.GenericInvoiceItem,
            dispatch_uid='waldur_mastermind.invoices.update_current_cost_when_invoice_item_is_updated_%s',
        )

        signals.post_delete.connect(
            handlers.update_current_cost_when_invoice_item_is_deleted,
            sender=models.GenericInvoiceItem,
            dispatch_uid='waldur_mastermind.invoices.update_current_cost_when_invoice_item_is_deleted',
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
