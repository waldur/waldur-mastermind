from django.apps import AppConfig


class PolicyConfig(AppConfig):
    name = "waldur_mastermind.policy"
    verbose_name = "Policy"

    def ready(self):
        from django.db.models import signals

        from waldur_core.core.utils import camel_case_to_underscore
        from waldur_mastermind.invoices import models as invoices_models
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.policy import handlers

        from . import models

        for klass in [
            models.ProjectEstimatedCostPolicy,
            models.CustomerEstimatedCostPolicy,
            models.OfferingEstimatedCostPolicy,
            models.OfferingUsagePolicy,
            models.SlurmPeriodicUsagePolicy,  # Add SLURM periodic policy to signal handling
            models.CustomerComponentUsagePolicy,
        ]:
            klass_name = camel_case_to_underscore(klass.__name__)

            if klass.trigger_class:
                # Handlers whose trigger_class is ComponentUsage are invoked
                # from process_component_usage_billing instead of from the
                # request thread, so they're not wired as direct post_save
                # listeners.
                if klass.trigger_class is marketplace_models.ComponentUsage:
                    continue
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
                    weak=False,  # Handler is a closure, needs strong reference
                )

        signals.post_save.connect(
            handlers.customer_credit_changed_handler,
            sender=invoices_models.CustomerCredit,
            dispatch_uid="customer_credit_changed_handler",
        )

        signals.post_save.connect(
            handlers.project_credit_changed_handler,
            sender=invoices_models.ProjectCredit,
            dispatch_uid="project_credit_changed_handler",
        )

        signals.m2m_changed.connect(
            handlers.customer_credit_offerings_list_changed_handler,
            sender=invoices_models.CustomerCredit.offerings.through,
            dispatch_uid="customer_credit_offerings_list_changed_handler",
        )

        signals.pre_delete.connect(
            handlers.run_reset_actions_upon_cost_policy_deletion,
            sender=models.ProjectEstimatedCostPolicy,
            dispatch_uid="waldur_mastermind.policy.run_reset_actions_upon_cost_policy_deletion",
        )

        # Connect to marketplace resource creation validation signal
        from waldur_mastermind.marketplace import signals as marketplace_signals

        marketplace_signals.resource_creation_validation.connect(
            handlers.validate_resource_creation_against_cost_policies,
            dispatch_uid="waldur_mastermind.policy.validate_resource_creation_against_cost_policies",
        )
