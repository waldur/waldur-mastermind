import logging

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.models import CustomerCredit, ProjectCredit
from waldur_mastermind.marketplace import models as marketplace_models

from . import models, utils
from .models import ProjectEstimatedCostPolicy

logger = logging.getLogger(__name__)


def customer_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    """Evaluate customer cost policies when invoice items are updated."""
    invoice_item = instance
    policies = models.CustomerEstimatedCostPolicy.objects.filter(
        scope=invoice_item.invoice.customer
    )
    if policies.count() > 0:
        logger.info(
            "Evaluating %s customer policies after invoice item update",
            policies.count(),
        )
        utils.evaluate_policies(policies)


def project_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    """Evaluate project cost policies when invoice items are updated."""
    invoice_item = instance
    policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope=invoice_item.project
    )
    if policies.count() > 0:
        logger.info(
            "Evaluating %s project policies after invoice item update", policies.count()
        )
        utils.evaluate_policies(policies)


def get_offering_trigger_handler(klass):
    def handler(sender, instance, created=False, **kwargs):
        resource = instance.resource

        if resource:
            policies = klass.objects.filter(
                scope=resource.offering,
                organization_groups__in=resource.project.customer.organization_groups.all(),
            )

            utils.evaluate_policies(policies)

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

        if getattr(observable_object, "is_mocked", False):
            return

        policies = klass.objects.filter(
            scope=klass.get_scope_from_observable_object(observable_object)
        )

        for policy in policies:
            if policy.get_threshold_actions() and policy.is_triggered():
                for action in policy.get_threshold_actions():
                    if action.ignored_fields and hasattr(observable_object, "tracker"):
                        if not set(observable_object.tracker.changed()) - set(
                            action.ignored_fields
                        ):
                            continue

                    action.method(policy, created)
                    logger.info(
                        "%s action has been triggered for %s. Policy UUID: %s",
                        action.method.__name__,
                        policy.scope.name,
                        policy.uuid.hex,
                    )

    return handler


def customer_credit_changed_handler(
    sender, instance: CustomerCredit, created=False, **kwargs
):
    """Handle customer credit value changes and evaluate related policies."""
    customer_credit = instance

    if not customer_credit.tracker.has_changed("value"):
        return

    logger.info(
        "%s has changed, looking up customer and project policies", customer_credit
    )
    customer_policies = models.CustomerEstimatedCostPolicy.objects.filter(
        scope=customer_credit.customer
    )
    if customer_policies.count() > 0:
        logger.info(
            "%s customer policies are found, evaluating them", customer_policies.count()
        )
        utils.evaluate_policies(customer_policies)
    else:
        logger.info("Customer policies are not found, skipping evaluation")

    project_policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope__customer=customer_credit.customer
    )
    if project_policies.count() > 0:
        logger.info(
            "%s project policies are found, evaluating them", customer_credit.customer
        )
        utils.evaluate_policies(project_policies)
    else:
        logger.info("Project policies are not found, skipping evaluation")


def project_credit_changed_handler(
    sender, instance: ProjectCredit, created=False, **kwargs
):
    project_credit = instance

    if not project_credit.tracker.has_changed("value"):
        return

    logger.info("%s has changed, looking up project policies", project_credit)
    project_policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope=project_credit.project
    )
    if project_policies.count() > 0:
        logger.info(
            "%s project policies are found, evaluating them", project_credit.project
        )
        utils.evaluate_policies(project_policies)
    else:
        logger.info("Project policies are not found, skipping evaluation")


def customer_credit_offerings_list_changed_handler(
    sender, instance, action, reverse, model, pk_set, **kwargs
):
    if action in ("post_add", "post_remove", "post_clear"):
        # Handle the case when pk_set is None (e.g., during clear() operation)
        if pk_set is None:
            # For clear operations, evaluate policies for the customer credit instance
            policies = models.CustomerEstimatedCostPolicy.objects.filter(
                scope_id=instance.customer_id
            )
        else:
            offerings = marketplace_models.Offering.objects.filter(pk__in=pk_set)
            customer_ids = invoices_models.CustomerCredit.objects.filter(
                offerings__in=offerings
            ).values_list("customer_id", flat=True)
            policies = models.CustomerEstimatedCostPolicy.objects.filter(
                scope_id__in=customer_ids
            )

        if policies.count() > 0:
            utils.evaluate_policies(policies)


def run_reset_actions_upon_cost_policy_deletion(
    sender, instance: ProjectEstimatedCostPolicy, **kwargs
) -> None:
    """
    Execute reset actions when a cost policy is deleted.

    Args:
        sender: The model class that sent the signal
        instance: The policy instance being deleted
        kwargs: Additional keyword arguments
    """
    policy: models.ProjectEstimatedCostPolicy = instance

    try:
        actions = policy.get_immediate_actions()
        for action in actions:
            reset_method = action.reset_method
            if reset_method:
                logger.info(
                    "Running immediate action reset method %s for policy %s (UUID: %s)",
                    reset_method.__name__,
                    policy.scope.name if policy.scope else "unknown",
                    policy.uuid.hex,
                )
                try:
                    reset_method(policy)
                except Exception as e:
                    logger.exception(
                        "Failed to execute reset method %s: %s",
                        reset_method.__name__,
                        str(e),
                    )
    except Exception as e:
        logger.exception(
            "Failed to run reset actions for policy %s: %s",
            getattr(policy, "uuid", "unknown"),
            str(e),
        )
