from django.apps import AppConfig
from django.db.models import signals


class SlurmInvoicesConfig(AppConfig):
    name = 'waldur_mastermind.slurm_invoices'
    verbose_name = 'Batch packages'

    def ready(self):
        from waldur_mastermind.invoices import registrators
        from waldur_slurm import models as slurm_models
        from . import handlers, registrators as slurm_registrators

        registrators.RegistrationManager.add_registrator(
            slurm_models.Allocation,
            slurm_registrators.AllocationRegistrator
        )

        signals.post_save.connect(
            handlers.add_new_allocation_to_invoice,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_slurm.handlers.add_new_allocation_to_invoice',
        )

        signals.post_save.connect(
            handlers.update_allocation_deposit,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_slurm.handlers.update_allocation_deposit',
        )

        signals.post_save.connect(
            handlers.terminate_invoice_when_allocation_cancelled,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_slurm.handlers.terminate_invoice_when_allocation_cancelled',
        )

        signals.pre_delete.connect(
            handlers.terminate_invoice_when_allocation_deleted,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_slurm.handlers.terminate_invoice_when_allocation_deleted',
        )

        signals.post_save.connect(
            handlers.update_invoice_item_on_allocation_usage_update,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_slurm.handlers.update_invoice_item_on_allocation_usage_update',
        )
