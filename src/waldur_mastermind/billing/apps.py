from django.apps import AppConfig
from django.db.models import signals


class BillingConfig(AppConfig):
    name = 'waldur_mastermind.billing'
    verbose_name = 'Billing'

    def ready(self):
        from waldur_mastermind.invoices import models as invoices_models

        from . import handlers, models

        for index, model in enumerate(models.PriceEstimate.get_estimated_models()):
            signals.post_save.connect(
                handlers.create_price_estimate,
                sender=model,
                dispatch_uid='waldur_mastermind.billing.'
                             'create_price_estimate_%s_%s' % (index, model.__class__),
            )

        for index, model in enumerate(models.PriceEstimate.get_estimated_models()):
            signals.pre_delete.connect(
                handlers.delete_stale_price_estimate,
                sender=model,
                dispatch_uid='waldur_mastermind.billing.'
                             'delete_stale_price_estimate_%s_%s' % (index, model.__class__),
            )

        signals.post_save.connect(
            handlers.update_estimate_when_invoice_is_created,
            sender=invoices_models.Invoice,
            dispatch_uid='waldur_mastermind.billing.'
                         'update_estimate_when_invoice_is_created',
        )

        signals.post_save.connect(
            handlers.process_invoice_item,
            sender=invoices_models.GenericInvoiceItem,
            dispatch_uid='waldur_mastermind.billing. process_invoice_item',
        )

        signals.post_save.connect(
            handlers.log_price_estimate_limit_update,
            sender=models.PriceEstimate,
            dispatch_uid='waldur_mastermind.billing.log_price_estimate_limit_update',
        )
