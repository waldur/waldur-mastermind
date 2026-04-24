import logging
from typing import cast

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.core.middleware import get_skip_side_effects
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_utils import (
    convert_quantity,
    get_component_details,
)
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
)

logger = logging.getLogger(__name__)


class BillingUsageProcessor:
    """
    Processor for handling billing based on reported component usage,
    including support for prepaid components and overage billing.
    """

    @classmethod
    @transaction.atomic
    def update_invoice_when_usage_is_reported(
        cls,
        sender,
        instance: marketplace_models.ComponentUsage,
        created=False,
        **kwargs,
    ):
        """
        Handles billing when component usage is reported, with integrated prepaid logic.

        This method acts as a dispatcher based on the component's configuration:

        1.  **Prepaid Components (`is_prepaid=True`):**
            - It checks the resource's prepaid balance for that component.
            - If usage is within the balance, it is considered "free" and no
              invoice item is generated. The usage is still recorded for tracking.
            - If usage exceeds the balance, it is split:
                a) The remaining balance is consumed at no cost.
                b) The excess (overage) is billed using a linked "overage component"
                   at a potentially different, premium rate.

        2.  **Standard Usage Components (`billing_type=USAGE`):**
            Processes usage reports for components with USAGE billing type.
            Creates or updates invoice items based on reported usage quantities.

            Only processes usage for USAGE billing type components - limit-based
            components can report usage but it's ignored for invoicing.

            Requires valid plan period for proper billing period calculation.
            Creates invoice items with usage-based quantities and pricing.

        Args:
            sender: Signal sender (model class)
            instance: ComponentUsage instance that triggered the signal
            created: Whether the usage record was just created
            **kwargs: Additional signal arguments
        """
        # Skip billing during import operations
        if get_skip_side_effects():
            logger.debug(
                "Skipping billing for component usage during import/maintenance mode"
            )
            return

        component_usage = instance
        resource = component_usage.resource
        offering_component = component_usage.component

        # Ignore signal if usage value has not changed
        if not created and not component_usage.tracker.has_changed("usage"):
            return

        plan_period = component_usage.plan_period
        if not plan_period:
            logger.warning(
                f"Skipping invoice item creation for resource '{resource.uuid}' because plan_period is not defined."
            )
            return

        logger.info(
            f"Processing usage report for resource '{resource.uuid}' and "
            f"component '{offering_component.type}'. Reported usage: {component_usage.usage}"
        )

        if offering_component.is_prepaid:
            cls._process_prepaid_usage(component_usage)
        elif offering_component.billing_type == BillingTypes.USAGE:
            cls._create_or_update_usage_invoice_item(
                resource=resource,
                offering_component=offering_component,
                usage_to_bill=component_usage.usage,
                date=component_usage.date,
                plan_period=plan_period,
            )

    @classmethod
    def _process_prepaid_usage(
        cls,
        component_usage: marketplace_models.ComponentUsage,
    ):
        resource = component_usage.resource
        offering_component = component_usage.component
        plan_period = cast(
            marketplace_models.ResourcePlanPeriod, component_usage.plan_period
        )
        reported_usage = int(component_usage.usage)

        prepaid_balance = resource.get_prepaid_balance(
            offering_component, excluded_ids=[component_usage.pk]
        )

        if reported_usage <= prepaid_balance:
            logger.info(
                f"Usage ({reported_usage}) is within prepaid balance ({prepaid_balance}) "
                f"for resource '{resource.uuid}'. No overage charge."
            )
            # The usage is recorded, but no billable invoice item is created.
            return

        else:
            # Usage exceeds balance; split it.
            overage_amount = reported_usage - prepaid_balance
            prepaid_consumed = prepaid_balance

            logger.info(
                f"Usage split for resource '{resource.uuid}': "
                f"{prepaid_consumed} consumed from prepaid balance. "
                f"{overage_amount} billed as overage."
            )

            if not offering_component.overage_component:
                logger.error(
                    f"Overage of {overage_amount} occurred for resource '{resource.uuid}', "
                    f"but no overage_component is configured on component '{offering_component.type}'. "
                    "Overage will not be billed."
                )
                return

            # We need to bill the overage amount against the overage component.
            cls._create_or_update_usage_invoice_item(
                resource=resource,
                offering_component=offering_component.overage_component,
                usage_to_bill=overage_amount,
                date=component_usage.date,
                plan_period=plan_period,
                is_overage=True,
            )

    @classmethod
    def _create_or_update_usage_invoice_item(
        cls,
        resource: marketplace_models.Resource,
        offering_component: marketplace_models.OfferingComponent,
        usage_to_bill,
        date,
        plan_period: marketplace_models.ResourcePlanPeriod,
        is_overage=False,
    ):
        """
        Helper method to create or update an invoice item for usage-based components.
        Handles both standard usage and overage billing.
        """

        from waldur_mastermind.marketplace.billing import MarketplaceBillingService

        plan = plan_period.plan
        customer = resource.project.customer
        invoice, _ = MarketplaceBillingService.get_or_create_invoice(customer, date)

        if invoice.state not in invoice_models.Invoice.States.MUTABLE_STATES:
            logger.warning(
                "Skipping usage update for resource '%s' and component '%s' "
                "because invoice %s-%02d is already in '%s' state.",
                resource.uuid,
                offering_component.type,
                invoice.year,
                invoice.month,
                invoice.state,
            )
            return

        # Try to find an existing invoice item for this component in the current period
        item = invoice.items.filter(
            resource=resource,
            details__offering_component_type=offering_component.type,
        ).first()

        try:
            plan_component = plan.components.get(component=offering_component)
            unit_price = plan_component.price
        except ObjectDoesNotExist:
            # PlanComponent not found - use price 0 to still track usage
            plan_component = None
            unit_price = 0
            if get_skip_side_effects():
                logger.debug(
                    f"PlanComponent for component '{offering_component.type}' not found "
                    f"in plan '{plan.name}' for resource '{resource.uuid}' during import. Using price 0."
                )
            else:
                logger.warning(
                    f"PlanComponent for component '{offering_component.type}' not found "
                    f"in plan '{plan.name}' for resource '{resource.uuid}'. Using price 0."
                )

        converted_usage = convert_quantity(
            usage_to_bill,
            resource.offering.type,
            offering_component.type,
            billing_type=offering_component.billing_type,
        )

        if item:
            item.quantity = converted_usage
            item.save(update_fields=["quantity"])
            logger.info(
                f"Updated invoice item {item.pk} for resource '{resource.uuid}'. New quantity: {item.quantity}"
            )
        else:
            # Create a new invoice item
            details = get_component_details(
                resource, plan_component, offering_component=offering_component
            )
            if is_overage:
                details["is_overage"] = True  # Add a flag for reporting

            month_start = core_utils.month_start(date)
            month_end = core_utils.month_end(date)

            start = (
                max(plan_period.start, month_start)
                if plan_period.start
                else month_start
            )
            end = min(plan_period.end, month_end) if plan_period.end else month_end

            item_name = f"{resource.name} / {offering_component.name}"
            if is_overage:
                item_name += " (Overage)"

            invoice_models.InvoiceItem.objects.create(
                resource=resource,
                plan_component=plan_component,
                project=resource.project,
                invoice=invoice,
                start=start,
                end=end,
                details=details,
                unit_price=unit_price,
                quantity=converted_usage,
                unit=common_mixins.UnitPriceMixin.Units.QUANTITY,
                measured_unit=offering_component.measured_unit,
                article_code=offering_component.article_code or plan.article_code,
                name=item_name,
            )
            logger.info(
                f"Created new invoice item for resource '{resource.uuid}' and "
                f"component '{offering_component.type}' with quantity {converted_usage}."
            )
