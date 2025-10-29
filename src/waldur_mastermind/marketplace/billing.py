import datetime
import logging

from django.db import transaction
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure.models import Customer
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_limit import LimitPeriodProcessor
from waldur_mastermind.marketplace.billing_utils import (
    get_component_details,
    get_component_name,
    get_invoice_item_name,
)
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.promotions import models as promotions_models
from waldur_mastermind.promotions.handlers import (
    create_discounted_resource_on_activation,
)

logger = logging.getLogger(__name__)


class MarketplaceBillingService:
    """
    Orchestrates billing for resource lifecycle events (creation, update, termination).

    This service acts as the central entry point for billing actions triggered by
    changes to a `marketplace.Resource` instance. It handles the billing logic for
    simple component types directly and delegates more complex scenarios to
    specialized processors:

    - Directly handles:
      - `FIXED`: Monthly recurring charges.
      - `ONE_TIME`: Single charge on resource creation.
      - `ON_PLAN_SWITCH`: Single charge when a resource's plan is changed.

    - Delegates to:
      - `LimitPeriodProcessor`: For `LIMIT` type components, which have complex
        period-based logic (e.g., monthly, quarterly, total).
      - `BillingUsageProcessor`: `USAGE` type components are handled entirely by
        this processor, triggered by usage reports, not by this service.
    """

    @classmethod
    def handle_resource_creation(cls, resource: marketplace_models.Resource):
        """Initializes billing when a resource is successfully provisioned."""
        cls._register(resource, order_type=OrderTypes.CREATE)

    @classmethod
    def handle_resource_termination(cls, resource: marketplace_models.Resource):
        """Finalizes billing when a resource is terminated."""
        cls._terminate(resource)

    @classmethod
    def handle_plan_change(cls, resource: marketplace_models.Resource):
        """Handles billing adjustments when a resource's plan is changed."""
        cls._terminate(resource)
        cls._register(resource, order_type=OrderTypes.UPDATE)

    @classmethod
    def handle_limits_change(cls, resource: marketplace_models.Resource):
        """Handles billing adjustments for limit changes by delegating to the LimitPeriodProcessor."""
        today = timezone.now()
        invoice, _ = MarketplaceBillingService.get_or_create_invoice(
            resource.project.customer, core_utils.month_start(today)
        )
        valid_limits = set(
            resource.offering.components.filter(
                billing_type=BillingTypes.LIMIT
            ).values_list("type", flat=True)
        )
        for component_type, new_quantity in resource.limits.items():
            if component_type not in valid_limits:
                continue
            LimitPeriodProcessor.process_update(
                resource, invoice, component_type, new_quantity
            )

    @classmethod
    def get_or_create_invoice(cls, customer: Customer, date: datetime.date, **kwargs):
        """
        Retrieves or creates an invoice for a customer for a specific month and year.

        If the invoice is newly created, it triggers billing processing for all
        of the customer's active resources for that month.
        """
        invoice, created = invoice_models.Invoice.objects.get_or_create(
            customer=customer,
            month=date.month,
            year=date.year,
        )

        if created:
            resources = (
                marketplace_models.Resource.objects.filter(project__customer=customer)
                .exclude(state__in=[ResourceStates.CREATING, ResourceStates.TERMINATED])
                .distinct()
            )
            end = core_utils.month_end(date)
            for resource in resources:
                cls._process_resource(
                    resource, invoice=invoice, start=date, end=end, **kwargs
                )

        return invoice, created

    @classmethod
    def _register(cls, resource: marketplace_models.Resource, now=None, **kwargs):
        """
        Initializes billing for a resource, creating invoice items for the current billing period.

        If an invoice for the current month does not exist, a new one is created.
        If it already exists, new invoice items for this resource are added to it.
        """
        if now is None:
            now = timezone.now()

        with transaction.atomic():
            invoice, created = cls.get_or_create_invoice(
                resource.project.customer, now, **kwargs
            )
            if not created:
                end = core_utils.month_end(now)
                cls._process_resource(
                    resource, invoice=invoice, start=now, end=end, **kwargs
                )

    @classmethod
    def _terminate(cls, resource: marketplace_models.Resource, now=None):
        """
        Ends the billing for all components of a resource, prorating the cost to the termination time.

        It finds all pending invoice items for the resource in the current month and
        updates their end time to `now`.
        """
        if now is None:
            now = timezone.now()

        with transaction.atomic():
            cls.get_or_create_invoice(resource.project.customer, now)
            items = invoice_models.InvoiceItem.objects.filter(
                resource=resource,
                invoice__customer=resource.project.customer,
                invoice__state=invoice_models.Invoice.States.PENDING,
                invoice__year=now.year,
                invoice__month=now.month,
                end=core_utils.month_end(now),
            )
            for item in items:
                item.terminate(end=now)

    @classmethod
    def _process_resource(
        cls,
        resource: marketplace_models.Resource,
        invoice: invoice_models.Invoice,
        start,
        end,
        **kwargs,
    ):
        """
        Creates invoice items for a resource by dispatching to the appropriate handler.

        This method iterates through the resource's plan components and acts as a
        dispatcher based on the offering component's billing type:

        - FIXED, ONE_TIME, ON_PLAN_SWITCH: Handled directly by `_process_simple_billing_component`.
        - LIMIT: Delegated to `LimitPeriodProcessor` for complex period-based logic.
        - USAGE: Ignored here. Usage-based billing is handled separately by
          `BillingUsageProcessor` when usage is reported.
        """
        plan = resource.plan

        if not plan:
            logger.warning(
                "Skipping an invoice creation because "
                "billing is not enabled for resource. "
                "Resource ID: %s",
                resource.id,
            )
            return

        order_type = kwargs.get("order_type")

        for plan_component in plan.components.all():
            offering_component = plan_component.component
            if not offering_component:
                logger.warning(
                    "Skipping an invoice item creation for resource %s because "
                    "offering component is not set.",
                    resource,
                )
                continue

            is_limit = offering_component.billing_type == BillingTypes.LIMIT

            if is_limit:
                LimitPeriodProcessor.process_creation(
                    resource, plan_component, invoice, start, end, order_type
                )
                continue
            else:
                cls._process_simple_billing_component(
                    resource, plan_component, invoice, start, end, order_type
                )

    @classmethod
    def _process_simple_billing_component(
        cls,
        resource: marketplace_models.Resource,
        plan_component: marketplace_models.PlanComponent,
        invoice: invoice_models.Invoice,
        start,
        end,
        order_type,
    ):
        """
        Creates an invoice item for simple, non-recurring, or fixed-recurring components.

        This handles the following billing types:
        - FIXED: Creates a recurring monthly item, prorated for the period.
        - ONE_TIME: Creates a single item if the order type is CREATE.
        - ON_PLAN_SWITCH: Creates a single item if the order type is UPDATE.
        """
        offering_component = plan_component.component

        is_fixed = offering_component.billing_type == BillingTypes.FIXED
        is_one = offering_component.billing_type == BillingTypes.ONE_TIME
        is_switch = offering_component.billing_type == BillingTypes.ON_PLAN_SWITCH
        if (
            is_fixed
            or (is_one and order_type == OrderTypes.CREATE)
            or (is_switch and order_type == OrderTypes.UPDATE)
        ):
            unit_price = plan_component.price
            unit = resource.plan.unit
            quantity = 0

            if is_fixed:
                unit_price *= plan_component.amount
                quantity = invoice_models.get_quantity(unit, start, end)
            elif is_one or is_switch:
                unit = invoice_models.Units.QUANTITY
                quantity = 1

            create_discounted_resource_on_activation(resource)

            # find campaigns
            (
                campaign,
                discounted_unit_price,
            ) = promotions_models.Campaign.get_discount_for_resource(
                resource=resource,
                year=invoice.year,
                month=invoice.month,
                unit_price=unit_price,
            )
            name = f"{get_invoice_item_name(resource)} / {get_component_name(plan_component)}"
            details = get_component_details(resource, plan_component)

            if campaign:
                name += " Discount."
                details["campaign_uuid"] = campaign.uuid.hex
                details["unit_price"] = float(unit_price)

            invoice_models.InvoiceItem.objects.create(
                name=name,
                details=details,
                resource=resource,
                project=resource.project,
                invoice=invoice,
                start=start,
                end=end,
                unit_price=discounted_unit_price,
                unit=unit,
                quantity=quantity,
                measured_unit=offering_component.measured_unit,
                article_code=offering_component.article_code
                or resource.plan.article_code,
            )
