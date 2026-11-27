from django.apps import AppConfig


def _on_component_usage_billed(usage, created: bool) -> None:
    from waldur_mastermind.marketplace import models as marketplace_models
    from waldur_mastermind.policy import handlers

    # Fire every handler that used to be wired directly to ComponentUsage
    # post_save in policy/apps.py (those with trigger_class=ComponentUsage).
    # These fire on every save to match the pre-MR semantics.
    handlers.offering_usage_policy_trigger_handler(
        sender=marketplace_models.ComponentUsage,
        instance=usage,
        created=created,
    )
    handlers.slurm_periodic_usage_policy_trigger_handler(
        sender=marketplace_models.ComponentUsage,
        instance=usage,
        created=created,
    )
    handlers.customer_component_usage_policy_trigger_handler(
        sender=marketplace_models.ComponentUsage,
        instance=usage,
        created=created,
    )


def _schedule_slurm_policy_evaluation(offering_id: int, resource_uuid: str) -> None:
    from waldur_mastermind.policy import models, tasks

    policies = models.SlurmPeriodicUsagePolicy.objects.filter(scope_id=offering_id)
    for policy in policies:
        tasks.evaluate_resource_against_policy.delay(resource_uuid, str(policy.uuid))


def _has_slurm_periodic_policy(offering) -> bool:
    from waldur_mastermind.policy import models

    return models.SlurmPeriodicUsagePolicy.objects.filter(scope=offering).exists()


def _snapshot_estimated_cost_policies(resource):
    from waldur_mastermind.policy import rebill_helpers

    return rebill_helpers.snapshot_estimated_cost_policies(resource)


def _get_policy_scope_label(policy) -> str:
    from waldur_mastermind.policy import rebill_helpers

    return rebill_helpers.get_policy_scope_label(policy)


def _preview_policy_action_resources(policy, action_name, meta, now_triggered):
    from waldur_mastermind.policy import rebill_helpers

    return rebill_helpers.preview_policy_action_resources(
        policy, action_name, meta, now_triggered
    )


class PolicyConfig(AppConfig):
    name = "waldur_mastermind.policy"
    verbose_name = "Policy"

    def ready(self):
        from django.db.models import signals

        from waldur_core.core.utils import camel_case_to_underscore
        from waldur_mastermind.invoices import models as invoices_models
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace import utils as marketplace_utils
        from waldur_mastermind.policy import handlers, policy_actions

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

        if policy_actions.policies_keep_paused not in marketplace_utils.PAUSE_KEEPERS:
            marketplace_utils.PAUSE_KEEPERS.append(policy_actions.policies_keep_paused)
        if (
            policy_actions.policies_keep_downscaled
            not in marketplace_utils.DOWNSCALE_KEEPERS
        ):
            marketplace_utils.DOWNSCALE_KEEPERS.append(
                policy_actions.policies_keep_downscaled
            )

        signals.post_delete.connect(
            handlers.lift_slurm_actions_upon_policy_deletion,
            sender=models.SlurmPeriodicUsagePolicy,
            dispatch_uid="waldur_mastermind.policy.lift_slurm_actions_upon_policy_deletion",
        )

        from waldur_mastermind.marketplace import hooks as marketplace_hooks

        marketplace_hooks.register_policy_provider(
            marketplace_hooks.PolicyProvider(
                on_component_usage_billed=_on_component_usage_billed,
                schedule_slurm_policy_evaluation=_schedule_slurm_policy_evaluation,
                has_slurm_periodic_policy=_has_slurm_periodic_policy,
                snapshot_estimated_cost_policies=_snapshot_estimated_cost_policies,
                get_policy_scope_label=_get_policy_scope_label,
                preview_policy_action_resources=_preview_policy_action_resources,
            )
        )
