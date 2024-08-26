from django.apps import AppConfig


class PolicyConfig(AppConfig):
    name = "waldur_mastermind.policy"
    verbose_name = "Policy"

    def ready(self):
        from django.db.models import signals

        from waldur_core.core.utils import camel_case_to_underscore
        from waldur_mastermind.policy import handlers

        from . import models

        for klass in [
            models.ProjectEstimatedCostPolicy,
            models.CustomerEstimatedCostPolicy,
            models.OfferingEstimatedCostPolicy,
            models.OfferingUsagePolicy,
        ]:
            klass_name = camel_case_to_underscore(klass.__name__)

            if klass.trigger_class:
                signals.post_save.connect(
                    getattr(handlers, f"{klass_name}_trigger_handler"),
                    sender=klass.trigger_class,
                    dispatch_uid=f"{klass_name}_handler",
                )

            for observable_klass in klass.observable_classes:
                signals.post_save.connect(
                    handlers.get_estimated_cost_policy_handler_for_observable_class(
                        klass, observable_klass
                    ),
                    sender=observable_klass,
                    dispatch_uid=f"{klass_name}_handler_for_observable_class",
                )
