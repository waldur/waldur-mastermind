import re

from django.apps import AppConfig


class PolicyConfig(AppConfig):
    name = "waldur_mastermind.policy"
    verbose_name = "Policy"

    def ready(self):
        from django.db.models import signals

        from waldur_mastermind.invoices import models as invoices_models
        from waldur_mastermind.policy import handlers

        from . import models

        for klass in [
            models.ProjectEstimatedCostPolicy,
            models.CustomerEstimatedCostPolicy,
        ]:
            if klass.trigger_class:
                signals.post_save.connect(
                    handlers.get_estimated_cost_policy_handler(klass),
                    sender=klass.trigger_class,
                    dispatch_uid="%s_handler"
                    % re.sub(r"(?<!^)(?=[A-Z])", "_", klass.__name__).lower(),
                )

        signals.post_save.connect(
            handlers.offering_estimated_cost_trigger_handler,
            sender=invoices_models.InvoiceItem,
            dispatch_uid="offering_estimated_cost_trigger_handler",
        )

        for klass in [
            models.ProjectEstimatedCostPolicy,
            models.CustomerEstimatedCostPolicy,
            models.OfferingEstimatedCostPolicy,
        ]:
            for observable_klass in klass.observable_classes:
                signals.post_save.connect(
                    handlers.get_estimated_cost_policy_handler_for_observable_class(
                        klass, observable_klass
                    ),
                    sender=observable_klass,
                    dispatch_uid="%s_handler_for_observable_class"
                    % re.sub(r"(?<!^)(?=[A-Z])", "_", klass.__name__).lower(),
                )
