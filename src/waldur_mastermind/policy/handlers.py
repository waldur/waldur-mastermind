import logging

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import models, utils

logger = logging.getLogger(__name__)


def customer_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    invoice_item = instance
    policies = models.CustomerEstimatedCostPolicy.objects.filter(
        scope=invoice_item.invoice.customer
    )
    utils.evaluate_policies(policies)


def project_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    invoice_item = instance
    policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope=invoice_item.project
    )
    utils.evaluate_policies(policies)


def get_offering_trigger_handler(klass):
    def handler(sender, instance, created=False, **kwargs):
        resource = instance.resource

        if resource:
            policies = klass.objects.filter(
                scope=resource.offering,
                organization_groups=resource.project.customer.organization_group,
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
        policies = klass.objects.filter(
            scope=klass.get_scope_from_observable_object(observable_object)
        )

        for policy in policies:
            if policy.get_threshold_actions() and policy.is_triggered():
                for action in policy.get_threshold_actions():
                    action.method(policy, created)
                    logger.info(
                        "%s action has been triggered for %s. Policy UUID: %s",
                        action.method.__name__,
                        policy.scope.name,
                        policy.uuid.hex,
                    )

    return handler


def run_policies_if_credit_has_been_changed(customer_credit):
    for klass in core_utils.get_all_subclasses(models.Policy):
        if klass.trigger_class != invoices_models.InvoiceItem:
            continue

        try:
            customer_path = klass.scope.field.related_model.Permissions.customer_path
            policies = klass.objects.filter(
                **{"scope__" + customer_path: customer_credit.customer}
            )
            utils.evaluate_policies(policies)
        except AttributeError:
            continue


def customer_credit_changed_handler(sender, instance, created=False, **kwargs):
    customer_credit = instance

    if not customer_credit.tracker.has_changed("value"):
        return

    policies = models.CustomerEstimatedCostPolicy.objects.filter(
        scope=customer_credit.customer
    )
    policies and utils.evaluate_policies(policies)

    policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope__customer=customer_credit.customer
    )
    policies and utils.evaluate_policies(policies)


def project_credit_changed_handler(sender, instance, created=False, **kwargs):
    project_credit = instance

    if not project_credit.tracker.has_changed("value"):
        return

    policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope__customer=project_credit.project.customer
    )
    policies and utils.evaluate_policies(policies)


def project_credit_offerings_list_changed_handler(
    sender, instance, action, reverse, model, pk_set, **kwargs
):
    if action in ("post_add", "post_remove", "post_clear"):
        offerings = marketplace_models.Offering.objects.filter(pk__in=pk_set)
        customer_ids = invoices_models.CustomerCredit.objects.filter(
            offerings__in=offerings
        ).values_list("customer_id", flat=True)

        policies = models.CustomerEstimatedCostPolicy.objects.filter(
            scope_id__in=customer_ids
        )
        policies and utils.evaluate_policies(policies)
