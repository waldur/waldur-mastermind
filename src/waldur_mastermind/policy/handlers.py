import logging

from django.utils import timezone

from . import models

logger = logging.getLogger(__name__)


def run_one_time_actions(policies):
    for policy in policies:
        if not policy.has_fired and policy.is_triggered():
            policy.has_fired = True
            policy.fired_datetime = timezone.now()
            policy.save()

            for action in policy.get_one_time_actions():
                action(policy)
                logger.info(
                    "%s action has been triggered for %s. Policy UUID: %s",
                    action.__name__,
                    policy.scope.name,
                    policy.uuid.hex,
                )

        elif policy.has_fired and not policy.is_triggered():
            policy.has_fired = False
            policy.save()


def customer_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    invoice_item = instance
    policies = models.CustomerEstimatedCostPolicy.objects.filter(
        scope=invoice_item.invoice.customer
    )
    run_one_time_actions(policies)


def project_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    invoice_item = instance
    policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope=invoice_item.project
    )
    run_one_time_actions(policies)


def get_offering_trigger_handler(klass):
    def handler(sender, instance, created=False, **kwargs):
        resource = instance.resource

        if resource:
            policies = klass.objects.filter(
                scope=resource.offering,
                organization_groups=resource.project.customer.organization_group,
            )

            run_one_time_actions(policies)

    return handler


offering_usage_policy_trigger_handler = get_offering_trigger_handler(
    models.OfferingUsagePolicy
)
offering_estimated_cost_policy_trigger_handler = get_offering_trigger_handler(
    models.OfferingEstimatedCostPolicy
)


def get_estimated_cost_policy_handler_for_observable_class(klass, observable_class):
    def handler(sender, instance, created=False, **kwargs):
        if not isinstance(instance, observable_class):
            return

        observable_object = instance
        policies = klass.objects.filter(
            scope=klass.get_scope_from_observable_object(observable_object)
        )

        for policy in policies:
            if policy.is_triggered():
                for action in policy.get_not_one_time_actions():
                    action(policy, created)
                    logger.info(
                        "%s action has been triggered for %s. Policy UUID: %s",
                        action.__name__,
                        policy.scope.name,
                        policy.uuid.hex,
                    )

    return handler
