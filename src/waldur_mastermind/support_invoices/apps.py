from django.apps import AppConfig
from django.db.models import signals


class SupportInvoicesConfig(AppConfig):
    name = 'waldur_mastermind.support_invoices'
    verbose_name = 'Support'

    def ready(self):
        from waldur_mastermind.invoices import registrators
        from waldur_mastermind.support import models as support_models
        from waldur_mastermind.marketplace import models as marketplace_models
        from . import handlers, registrators as support_registrators

        registrators.RegistrationManager.add_registrator(
            support_models.Offering,
            support_registrators.OfferingRegistrator
        )

        signals.post_save.connect(
            handlers.add_new_offering_to_invoice,
            sender=support_models.Offering,
            dispatch_uid='support_invoices.handlers.add_new_offering_to_invoice',
        )

        signals.post_save.connect(
            handlers.terminate_invoice_when_offering_cancelled,
            sender=support_models.Offering,
            dispatch_uid='support_invoices.handlers.terminate_invoice_when_offering_cancelled',
        )

        signals.pre_delete.connect(
            handlers.terminate_invoice_when_offering_deleted,
            sender=support_models.Offering,
            dispatch_uid='support_invoices.handlers.terminate_invoice_when_offering_deleted',
        )

        signals.post_save.connect(
            handlers.switch_plan_resource,
            sender=marketplace_models.Resource,
            dispatch_uid='support_invoices.handlers.switch_plan_resource',
        )

        signals.post_save.connect(
            handlers.update_invoice_item,
            sender=marketplace_models.ComponentUsage,
            dispatch_uid='support_invoices.handlers.update_invoice_item',
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
