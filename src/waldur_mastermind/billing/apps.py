from django.apps import AppConfig
from django.db.models import signals


class BillingConfig(AppConfig):
    name = "waldur_mastermind.billing"
    verbose_name = "Billing"

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.structure import serializers as structure_serializers
        from waldur_mastermind.billing.serializers import add_price_estimate
        from waldur_mastermind.invoices import models as invoices_models
        from waldur_mastermind.policy import serializers as policy_serializers

        from . import handlers, models

        for index, model in enumerate(models.PriceEstimate.get_estimated_models()):
            signals.post_save.connect(
                handlers.create_price_estimate,
                sender=model,
                dispatch_uid="waldur_mastermind.billing."
                f"create_price_estimate_{index}_{model.__class__}",
            )

        for index, model in enumerate(models.PriceEstimate.get_estimated_models()):
            signals.pre_delete.connect(
                handlers.delete_stale_price_estimate,
                sender=model,
                dispatch_uid="waldur_mastermind.billing."
                f"delete_stale_price_estimate_{index}_{model.__class__}",
            )

        signals.post_save.connect(
            handlers.update_estimate_when_invoice_is_created,
            sender=invoices_models.Invoice,
            dispatch_uid="waldur_mastermind.billing."
            "update_estimate_when_invoice_is_created",
        )

        signals.post_save.connect(
            handlers.process_invoice_item,
            sender=invoices_models.InvoiceItem,
            dispatch_uid="waldur_mastermind.billing.process_invoice_item",
        )

        core_signals.pre_serializer_fields.connect(
            sender=structure_serializers.ProjectSerializer,
            receiver=add_price_estimate,
        )

        core_signals.pre_serializer_fields.connect(
            sender=policy_serializers.ProjectEstimatedCostPolicySerializer,
            receiver=add_price_estimate,
        )
