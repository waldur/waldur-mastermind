from django.apps import AppConfig
from django.db.models import signals


class SupportInvoicesConfig(AppConfig):
    name = 'waldur_mastermind.support_invoices'
    verbose_name = 'Support'

    def ready(self):
        from waldur_mastermind.invoices import registrators
        from waldur_mastermind.support import models as support_models
        from waldur_mastermind.marketplace import models as marketplace_models
        from . import handlers, registrators as support_registrators, models

        registrators.RegistrationManager.add_registrator(
            models.RequestBasedOffering,
            support_registrators.OfferingRegistrator
        )

        signals.post_save.connect(
            handlers.add_new_offering_to_invoice,
            sender=support_models.Offering,
            dispatch_uid='waldur_slurm.handlers.add_new_offering_to_invoice',
        )

        signals.post_save.connect(
            handlers.terminate_invoice_when_offering_cancelled,
            sender=support_models.Offering,
            dispatch_uid='waldur_slurm.handlers.terminate_invoice_when_offering_cancelled',
        )

        signals.pre_delete.connect(
            handlers.terminate_invoice_when_offering_deleted,
            sender=support_models.Offering,
            dispatch_uid='waldur_slurm.handlers.terminate_invoice_when_offering_deleted',
        )

        signals.post_save.connect(
            handlers.update_invoice_item_on_component_usage_create,
            sender=marketplace_models.ComponentUsage,
            dispatch_uid='waldur_slurm.handlers.update_invoice_item_on_component_usage_create',
        )
