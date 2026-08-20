from django.apps import AppConfig
from django.db.models import signals


class InvoiceConfig(AppConfig):
    name = "waldur_mastermind.invoices"
    verbose_name = "Invoices"

    def ready(self):
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals

        from . import handlers, models

        signals.pre_save.connect(
            handlers.set_tax_percent_on_invoice_creation,
            sender=models.Invoice,
            dispatch_uid="waldur_mastermind.invoices.set_tax_percent_on_invoice_creation",
        )

        signals.post_save.connect(
            handlers.log_invoice_state_transition,
            sender=models.Invoice,
            dispatch_uid="waldur_mastermind.invoices.log_invoice_state_transition",
        )

        signals.post_save.connect(
            handlers.emit_invoice_created_event,
            sender=models.Invoice,
            dispatch_uid="waldur_mastermind.invoices.emit_invoice_created_event",
        )

        signals.post_save.connect(
            handlers.set_project_name_on_invoice_item_creation,
            sender=models.InvoiceItem,
            dispatch_uid="waldur_mastermind.invoices.set_project_name_on_invoice_item_creation",
        )

        signals.post_save.connect(
            handlers.update_cache_when_invoice_item_is_updated,
            sender=models.InvoiceItem,
            dispatch_uid="waldur_mastermind.invoices.update_cache_when_invoice_item_is_updated_%s",
        )

        signals.post_delete.connect(
            handlers.update_cache_when_invoice_item_is_deleted,
            sender=models.InvoiceItem,
            dispatch_uid="waldur_mastermind.invoices.update_cache_when_invoice_item_is_deleted",
        )

        signals.post_save.connect(
            handlers.update_invoice_item_on_project_name_update,
            sender=structure_models.Project,
            dispatch_uid="waldur_mastermind.invoices.update_invoice_item_on_project_name_update",
        )

        structure_signals.project_moved.connect(
            handlers.projects_customer_has_been_changed,
            sender=structure_models.Project,
            dispatch_uid="waldur_mastermind.invoices.projects_customer_has_been_changed",
        )

        signals.post_save.connect(
            handlers.create_carried_over_usage_if_invoice_has_been_created,
            sender=models.Invoice,
            dispatch_uid="waldur_mastermind.invoices.create_carried_over_usage_if_invoice_has_been_created",
        )

        signals.post_save.connect(
            handlers.log_credit,
            sender=models.CustomerCredit,
            dispatch_uid="waldur_mastermind.invoices.log_credit",
        )

        signals.post_save.connect(
            handlers.record_credit_transaction,
            sender=models.CustomerCredit,
            dispatch_uid="waldur_mastermind.invoices.record_credit_transaction",
        )

        signals.post_save.connect(
            handlers.record_credit_transaction,
            sender=models.ProjectCredit,
            dispatch_uid="waldur_mastermind.invoices.record_project_credit_transaction",
        )

        signals.post_save.connect(
            handlers.log_affiliate,
            sender=models.CustomerAffiliate,
            dispatch_uid="waldur_mastermind.invoices.log_affiliate",
        )

        from . import signals as cost_signals

        cost_signals.invoice_created.connect(
            handlers.process_affiliate_fees,
            sender=models.Invoice,
            dispatch_uid="waldur_mastermind.invoices.process_affiliate_fees",
        )

        signals.pre_delete.connect(
            handlers.delete_project_credits_with_customer_credit,
            sender=models.CustomerCredit,
            dispatch_uid="waldur_mastermind.invoices.delete_project_credits_with_customer_credit",
        )

        signals.post_save.connect(
            handlers.log_project_credit,
            sender=models.ProjectCredit,
            dispatch_uid="waldur_mastermind.invoices.log_project_credit",
        )

        signals.post_save.connect(
            handlers.log_invoice_item_save,
            sender=models.InvoiceItem,
            dispatch_uid="waldur_mastermind.invoices.log_invoice_item_save",
        )

        signals.post_delete.connect(
            handlers.log_invoice_item_delete,
            sender=models.InvoiceItem,
            dispatch_uid="waldur_mastermind.invoices.log_invoice_item_delete",
        )

        signals.pre_delete.connect(
            handlers.refund_project_credit_on_project_removal,
            sender=structure_models.Project,
            dispatch_uid="waldur_mastermind.invoices.refund_project_credit_on_project_removal",
        )

        from waldur_core.core.handlers import create_initial_revision

        signals.post_save.connect(
            create_initial_revision,
            sender=models.Invoice,
            dispatch_uid="waldur_mastermind.invoices.create_initial_revision_Invoice",
        )
