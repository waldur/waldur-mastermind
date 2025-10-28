import datetime
import logging
from decimal import Decimal
from typing import cast

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q, signals
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure.models import Customer
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    SLURM_OFFERING,
    BillingTypes,
    LimitPeriods,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace_openstack import CORES_TYPE, RAM_TYPE, STORAGE_TYPE
from waldur_mastermind.promotions import models as promotions_models
from waldur_openstack.utils import is_valid_volume_type_name

logger = logging.getLogger(__name__)


class LimitPeriodProcessor:
    """
    Encapsulates the business logic for handling different limit periods
    (e.g., MONTH, QUARTERLY, ANNUAL, TOTAL) for limit-based components.
    """

    @classmethod
    def process_creation(
        cls,
        resource: marketplace_models.Resource,
        plan_component: marketplace_models.PlanComponent,
        invoice: invoice_models.Invoice,
        start,
        end,
        order_type,
    ):
        """
        Processes the creation of invoice items for limit-based components,
        handling different limit periods.
        """
        offering_component = plan_component.component
        limit_period = offering_component.limit_period

        if limit_period == LimitPeriods.TOTAL:
            # For TOTAL, only bill on resource creation.
            if order_type == OrderTypes.CREATE:
                MarketplaceBillingService.create_component_item(
                    resource, plan_component, invoice, start, end
                )
            return

        if not cls.should_process_billing(limit_period, start):
            # Skip billing for this period (e.g., non-quarterly month for a quarterly component)
            return

        # Use the appropriate billing period instead of the default monthly one
        if limit_period == LimitPeriods.QUARTERLY:
            billing_start, billing_end = (
                core_utils.get_quarter_start(start),
                core_utils.get_quarter_end(start),
            )
        else:
            # For MONTH, ANNUAL, etc., use the period provided by the caller.
            billing_start, billing_end = start, end

        MarketplaceBillingService.create_component_item(
            resource,
            plan_component,
            invoice,
            billing_start,
            billing_end,
        )

    @classmethod
    def process_update(
        cls,
        resource: marketplace_models.Resource,
        invoice: invoice_models.Invoice,
        component_type: str,
        new_quantity: int,
    ):
        """
        Processes the update of invoice items when resource limits change,
        dispatching to the correct handler based on the limit period.
        """
        offering_component = resource.offering.components.get(type=component_type)
        if offering_component.limit_period == LimitPeriods.TOTAL:
            MarketplaceBillingService.create_invoice_item_for_total_limit(
                resource,
                invoice,
                component_type,
                new_quantity,
                offering_component,
            )
        else:
            MarketplaceBillingService.create_or_update_component_item(
                resource, invoice, component_type, new_quantity
            )

    @classmethod
    def get_billing_period(
        cls, limit_period: str, date: datetime.date
    ) -> tuple[datetime.datetime, datetime.datetime]:
        """
        Get the full billing period (start, end) for a given limit period
        containing the given date.
        """
        if limit_period == LimitPeriods.QUARTERLY:
            return core_utils.get_quarter_start(date), core_utils.get_quarter_end(date)
        # Default to monthly boundaries for other recurring limit types (MONTH, ANNUAL)
        # when creating a new item mid-cycle.
        return core_utils.month_start(date), core_utils.month_end(date)

    @classmethod
    def should_process_billing(cls, limit_period: str, date: datetime.date) -> bool:
        """
        Check if billing should be processed for the given date based on the limit period.
        """
        if limit_period == LimitPeriods.QUARTERLY:
            # Quarterly billing should only happen in the first month of each quarter:
            # January (Q1), April (Q2), July (Q3), October (Q4)
            return date.month in [1, 4, 7, 10]
        # MONTH, ANNUAL, TOTAL are processed every month.
        return True


class MarketplaceBillingService:
    """
    It handles billing for different component types.

    This service manages invoice generation for marketplace resources based on
    their offering components' billing types:

    - FIXED: Fixed-price components billed monthly with prorated quantities
    - USAGE: Usage-based components billed based on reported usage
    - LIMIT: Limit-based components billed based on user-specified limits
    - ONE_TIME: One-time components billed once during resource creation
    - ON_PLAN_SWITCH: Components billed when switching between plans

    The service handles different billing scenarios:
    - Monthly billing for fixed components
    - Usage reporting for dynamic components
    - Limit updates with prorated billing periods
    - Plan switching with appropriate billing adjustments
    - Campaign discounts and promotional pricing

    Billing behavior varies by limit period for limit-based components:
    - MONTH: Billed monthly based on limits
    - QUARTERLY: Billed quarterly based on limits
    - ANNUAL: Billed annually based on limits
    - TOTAL: One-time billing for total limit amount
    """

    @classmethod
    def get_or_create_invoice(cls, customer: Customer, date: datetime.date, **kwargs):
        invoice, created = invoice_models.Invoice.objects.get_or_create(
            customer=customer,
            month=date.month,
            year=date.year,
        )

        if created:
            sources = (
                marketplace_models.Resource.objects.filter(project__customer=customer)
                .exclude(state__in=[ResourceStates.CREATING, ResourceStates.TERMINATED])
                .distinct()
            )
            end = core_utils.month_end(date)
            for source in sources:
                MarketplaceBillingService()._create_item(
                    source, invoice=invoice, start=date, end=end, **kwargs
                )

        return invoice, created

    @classmethod
    def register(cls, resource: marketplace_models.Resource, now=None, **kwargs):
        """
        Create new invoice item from source and register it into invoice.

        If invoice does not exist new one will be created.
        """
        if now is None:
            now = timezone.now()

        with transaction.atomic():
            invoice, created = cls.get_or_create_invoice(
                resource.project.customer, now, **kwargs
            )
            if not created:
                end = core_utils.month_end(now)
                MarketplaceBillingService()._create_item(
                    resource, invoice=invoice, start=now, end=end, **kwargs
                )

    @classmethod
    def terminate(cls, resource: marketplace_models.Resource, now=None):
        """
        Terminate invoice item that corresponds to given source.

        :param source: invoice item to terminate.
        :param now: time to set as end of item usage.
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

    def _create_item(
        self,
        resource: marketplace_models.Resource,
        invoice: invoice_models.Invoice,
        start,
        end,
        **kwargs,
    ):
        """
        Create invoice items for a marketplace resource based on billing types.

        This method handles the core billing logic for different component types:

        1. FIXED billing: Creates monthly recurring charges with prorated quantities
        2. ONE_TIME billing: Creates one-time charges during resource creation
        3. ON_PLAN_SWITCH billing: Creates charges when switching between plans
        4. LIMIT billing: Creates charges based on user-specified limits with
           different behaviors for MONTH/QUARTERLY/ANNUAL/TOTAL limit periods
        5. USAGE billing: Handled separately in update_invoice_when_usage_is_reported

        For LIMIT billing with TOTAL period, charges are only created during
        resource creation to avoid recurring billing.

        Args:
            source: Marketplace resource to create invoice items for
            invoice: Invoice to add items to
            start: Billing period start date
            end: Billing period end date
            **kwargs: Additional arguments including order_type
        """
        resource = resource
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

            is_fixed = offering_component.billing_type == BillingTypes.FIXED
            is_one = offering_component.billing_type == BillingTypes.ONE_TIME
            is_switch = offering_component.billing_type == BillingTypes.ON_PLAN_SWITCH
            is_limit = offering_component.billing_type == BillingTypes.LIMIT

            if is_limit:
                LimitPeriodProcessor.process_creation(
                    resource, plan_component, invoice, start, end, order_type
                )
                continue

            if (
                is_fixed
                or (is_one and order_type == OrderTypes.CREATE)
                or (is_switch and order_type == OrderTypes.UPDATE)
            ):
                unit_price = plan_component.price
                unit = plan.unit
                quantity = 0

                if is_fixed:
                    unit_price *= plan_component.amount
                    quantity = invoice_models.get_quantity(unit, start, end)
                elif is_one or is_switch:
                    unit = invoice_models.Units.QUANTITY
                    quantity = 1

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
                name = f"{MarketplaceBillingService.get_name(resource)} / {self.get_component_name(plan_component)}"
                details = self.get_component_details(resource, plan_component)

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
                    article_code=offering_component.article_code or plan.article_code,
                )

    @classmethod
    def get_component_details(
        cls,
        resource: marketplace_models.Resource,
        plan_component: marketplace_models.PlanComponent,
    ):
        """
        Generate detailed metadata for invoice items.

        Creates comprehensive details object containing resource, plan, offering,
        and component information for invoice line items. This metadata is used
        for reporting, analytics, and invoice item identification.

        Args:
            resource: Marketplace resource being billed
            plan_component: Plan component being billed

        Returns:
            Dictionary containing detailed metadata for the invoice item
        """
        customer = resource.offering.customer
        service_provider = getattr(customer, "serviceprovider", None)

        return {
            "resource_name": resource.name,
            "resource_uuid": resource.uuid.hex,
            "plan_name": resource.plan.name if resource.plan else "",
            "plan_uuid": resource.plan.uuid.hex if resource.plan else "",
            "offering_type": resource.offering.type,
            "offering_name": resource.offering.name,
            "offering_uuid": resource.offering.uuid.hex,
            "service_provider_name": customer.name,
            "service_provider_uuid": ""
            if not service_provider
            else service_provider.uuid.hex,
            "plan_component_id": plan_component.id,
            "offering_component_type": plan_component.component.type,
            "offering_component_name": plan_component.component.name,
        }

    @classmethod
    def get_name(cls, resource: marketplace_models.Resource):
        """
        Generate display name for invoice items.

        Creates a descriptive name combining resource name, offering name,
        and plan name (if available) for invoice line items.

        Args:
            resource: Marketplace resource being billed

        Returns:
            String representation for invoice item display
        """
        if resource.plan:
            return f"{resource.name} ({resource.offering.name} / {resource.plan.name})"
        else:
            return f"{resource.name} ({resource.offering.name})"

    @classmethod
    def get_total_quantity(cls, unit, value, start, end):
        """
        Calculate total quantity for billing period based on unit type.

        For per-day billing, multiplies value by number of days in period.
        For other units, returns value as-is.

        Args:
            unit: Billing unit type (PER_DAY, etc.)
            value: Base value to calculate from
            start: Period start date
            end: Period end date

        Returns:
            Total quantity for the billing period
        """
        if unit == invoice_models.InvoiceItem.Units.PER_DAY:
            return value * get_full_days(start, end)
        return value

    @classmethod
    def create_component_item(
        cls,
        source: marketplace_models.Resource,
        plan_component: marketplace_models.PlanComponent,
        invoice: invoice_models.Invoice,
        start,
        end,
    ):
        """
        Create invoice item for limit-based components with separate discount item.

        Creates invoice items for components with LIMIT billing type based on
        resource limits. Handles different limit periods (MONTH, QUARTERLY, ANNUAL, TOTAL)
        and calculates appropriate quantities and units.

        For TOTAL limit period, uses QUANTITY unit instead of plan unit.
        Includes resource limit periods in details for tracking.

        Args:
            source: Marketplace resource being billed
            plan_component: Plan component to create item for
            invoice: Invoice to add item to
            start: Billing period start date
            end: Billing period end date
        """
        offering_component = plan_component.component
        if not offering_component:
            logger.warning(
                "Skipping invoice item creation for resource %s because "
                "offering component is not set.",
                source,
            )
            return

        # Additional safeguard: TOTAL period components should only be billed once
        # This prevents the bug where TOTAL components get billed multiple times
        if (
            offering_component.billing_type == BillingTypes.LIMIT
            and offering_component.limit_period == LimitPeriods.TOTAL
        ):
            # Check if this component has already been billed for this resource
            existing_items = invoice_models.InvoiceItem.objects.filter(
                resource=source,
                details__offering_component_type=offering_component.type,
            )
            if existing_items.exists():
                logger.warning(
                    "Prevented duplicate billing: TOTAL period component %s on resource %s "
                    "already has existing invoice items. TOTAL components should only be billed once.",
                    offering_component.type,
                    source.id,
                )
                return
        limit = source.limits.get(offering_component.type, 0)
        if not limit or limit == -1:
            return
        details = cls.get_component_details(source, plan_component)
        quantity = cls.convert_quantity(
            limit, source.offering.type, offering_component.type
        )
        details["resource_limit_periods"] = [
            utils.serialize_resource_limit_period(start, end, quantity)
        ]
        total_quantity = cls.get_total_quantity(
            plan_component.plan.unit, quantity, start, end
        )

        unit = plan_component.plan.unit
        if (
            offering_component.billing_type == BillingTypes.LIMIT
            and offering_component.limit_period == LimitPeriods.TOTAL
        ):
            unit = invoice_models.Units.QUANTITY

        invoice_models.InvoiceItem.objects.create(
            name=f"{cls.get_name(source)} / {cls.get_component_name(plan_component)}",
            resource=source,
            project=source.project,
            unit_price=plan_component.price,
            unit=unit,
            quantity=total_quantity,
            article_code=offering_component.article_code or source.plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
            measured_unit=offering_component.measured_unit,
        )

        # Check if discount applies and create separate discount item
        discount_amount, discount_applies = cls.calculate_discount_amount(
            plan_component, total_quantity, plan_component.price
        )

        if discount_applies:
            cls.create_discount_invoice_item(
                resource=source,
                plan_component=plan_component,
                invoice=invoice,
                discount_amount=discount_amount,
                quantity=total_quantity,
                start=start,
                end=end,
            )

    @classmethod
    def update_component_item(
        cls,
        resource: marketplace_models.Resource,
        component_type,
        invoice,
        new_quantity,
    ):
        """
        Update existing invoice item when resource limits change.

        Updates invoice items for limit-based components when resource limits
        are modified. Creates time-based billing periods to handle limit changes
        during the billing period with appropriate prorating.

        Args:
            source: Marketplace resource being updated
            component_type: Type of component being updated
            invoice: Invoice containing the item to update
            new_quantity: New limit quantity
        """
        if not resource.plan:
            return

        invoice_item = invoice_models.InvoiceItem.objects.get(
            resource=resource,
            details__offering_component_type=component_type,
            details__plan_uuid=str(resource.plan.uuid),
            invoice=invoice,
            unit_price__gte=0,  # exclude compensation items
        )
        resource_limit_periods = invoice_item.details["resource_limit_periods"]
        old_period = resource_limit_periods.pop()
        old_quantity = int(old_period["quantity"])
        old_start = parse_datetime(old_period["start"])
        today = timezone.now()
        new_quantity = cls.convert_quantity(
            new_quantity, resource.offering.type, component_type
        )
        if old_quantity == new_quantity:
            # Skip update if limit is the same
            return
        if old_quantity > new_quantity:
            old_end = today.replace(hour=23, minute=59, second=59)
            new_start = old_end + datetime.timedelta(seconds=1)
        else:
            new_start = today.replace(hour=0, minute=0, second=0)
            old_end = new_start - datetime.timedelta(seconds=1)
        old_period = utils.serialize_resource_limit_period(
            old_start, old_end, old_quantity
        )
        # Get the offering component to determine appropriate period end
        offering_component = resource.offering.components.get(type=component_type)
        _, period_end = LimitPeriodProcessor.get_billing_period(
            offering_component.limit_period, timezone.now()
        )

        new_period = utils.serialize_resource_limit_period(
            new_start, period_end, new_quantity
        )
        resource_limit_periods.extend([old_period, new_period])
        plan_component = resource.plan.components.get(component__type=component_type)
        invoice_item.quantity = sum(
            cls.get_total_quantity(
                plan_component.plan.unit,
                period["quantity"],
                parse_datetime(period["start"]),
                parse_datetime(period["end"]),
            )
            for period in resource_limit_periods
        )
        invoice_item.save(update_fields=["details", "quantity"])

    @classmethod
    @transaction.atomic
    def create_or_update_component_item(
        cls,
        resource: marketplace_models.Resource,
        invoice: invoice_models.Invoice,
        component_type,
        quantity,
    ):
        """
        Create or update invoice item for limit-based components.

        Determines whether to create a new invoice item or update an existing
        one based on whether an item already exists for the component type.
        Used when resource limits change during billing periods.

        Args:
            source: Marketplace resource being processed
            invoice: Invoice to create/update item in
            component_type: Type of component being processed
            quantity: Limit quantity for the component
        """
        if not resource.plan:
            logger.warning(
                "Skipping processing of invoice item %s because "
                "billing is not enabled for resource.",
                component_type,
            )
            return
        if invoice_models.InvoiceItem.objects.filter(
            resource=resource,
            details__offering_component_type=component_type,
            invoice=invoice,
        ).exists():
            cls.update_component_item(resource, component_type, invoice, quantity)
        else:
            now = timezone.now()
            try:
                plan_component = resource.plan.components.get(
                    component__type=component_type
                )
                # Get the offering component to determine appropriate period end
                offering_component = plan_component.component

                start, end = LimitPeriodProcessor.get_billing_period(
                    offering_component.limit_period, now
                )

            except ObjectDoesNotExist:
                logger.warning(
                    "Skipping processing of invoice item %s because "
                    "plan component is not defined.",
                    component_type,
                )
                return
            else:
                cls.create_component_item(resource, plan_component, invoice, start, end)

    @classmethod
    def create_discounted_resource(
        cls, sender, instance: marketplace_models.Resource, created=False, **kwargs
    ):
        """
        Create discounted resource associations for active campaigns.

        Processes resource against active campaigns when resource state changes
        to OK. Creates DiscountedResource records for applicable campaigns
        based on campaign conditions and coupon codes.

        Args:
            sender: Signal sender (model class)
            instance: Resource instance that triggered the signal
            created: Whether the resource was just created
            **kwargs: Additional signal arguments
        """
        resource = instance
        resource_tracker = resource.tracker

        if created:
            return

        if not resource_tracker.has_changed("state"):
            return

        if instance.state != ResourceStates.OK:
            return

        order = resource.creation_order

        if not order:
            return

        coupon = order.attributes.get("coupon", "")

        for campaign in promotions_models.Campaign.objects.filter(
            state=promotions_models.Campaign.States.ACTIVE,
            start_date__lte=resource.created,
            end_date__gte=resource.created,
        ).filter(Q(coupon="") | Q(coupon=coupon)):
            if campaign.check_resource_on_conditions_of_campaign(resource):
                promotions_models.DiscountedResource.objects.get_or_create(
                    campaign=campaign,
                    resource=resource,
                )

    @classmethod
    def on_resource_post_save(
        cls, sender, instance: marketplace_models.Resource, created=False, **kwargs
    ):
        """
        Handle resource state changes and billing events.

        Main signal handler for resource lifecycle events that trigger billing:

        1. Resource creation (CREATING -> OK): Registers resource for billing
        2. Resource termination (TERMINATING -> TERMINATED): Terminates billing
        3. Plan changes: Terminates old plan and registers new plan
        4. Limit changes: Updates invoice items for limit-based components

        For limit changes, handles different limit periods:
        - TOTAL: Creates one-time charges for total limit increases
        - MONTH/QUARTERLY/ANNUAL: Updates recurring charges for limit changes

        Args:
            sender: Signal sender (model class)
            instance: Resource instance that triggered the signal
            created: Whether the resource was just created
            **kwargs: Additional signal arguments
        """
        resource = instance
        if created:
            return

        resource_tracker = resource.tracker
        instance_tracker = instance.tracker

        if (
            resource.state == ResourceStates.OK
            and resource_tracker.previous("state") == ResourceStates.CREATING
        ):
            cls.create_discounted_resource(sender, instance, created)
            MarketplaceBillingService.register(
                resource, timezone.now(), order_type=OrderTypes.CREATE
            )

        if (
            resource.state == ResourceStates.TERMINATED
            and instance_tracker.previous("state") == ResourceStates.TERMINATING
        ):
            MarketplaceBillingService.terminate(resource, timezone.now())

        if resource.state != ResourceStates.CREATING and resource_tracker.has_changed(
            "plan_id"
        ):
            MarketplaceBillingService.terminate(resource, timezone.now())
            MarketplaceBillingService.register(
                resource, timezone.now(), order_type=OrderTypes.UPDATE
            )

        if resource.state != ResourceStates.CREATING and resource_tracker.has_changed(
            "limits"
        ):
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
    def create_invoice_item_for_total_limit(
        cls,
        resource: marketplace_models.Resource,
        invoice: invoice_models.Invoice,
        component_type: str,
        new_quantity: int,
        offering_component: marketplace_models.OfferingComponent,
    ):
        """
        Create invoice item for total limit component changes.

        Handles billing for limit-based components with TOTAL limit period.
        Creates incremental charges (positive or negative) based on the
        difference between new quantity and previously billed quantities.

        Uses QUANTITY unit for billing and creates compensation items
        (negative unit_price) for limit decreases.

        Args:
            resource: Marketplace resource being processed
            invoice: Invoice to create item in
            component_type: Type of component being processed
            new_quantity: New total limit quantity
            offering_component: Offering component configuration
        """
        if resource.state != ResourceStates.OK:
            return
        if not resource.plan:
            return
        related_invoice_items = invoice_models.InvoiceItem.objects.filter(
            resource=resource,
            details__offering_component_type=component_type,
        )
        if not related_invoice_items.exists():
            # For TOTAL period components, if no previous billing exists,
            # this is likely due to missing CREATE order billing.
            # In this case, bill the full amount to recover proper billing state.
            logger.info(
                "No existing invoice items found for TOTAL period component %s "
                "on resource %s. Billing full amount (%s) to recover from missing CREATE billing.",
                component_type,
                resource.id,
                new_quantity,
            )
            total = 0  # Start from 0 since no previous billing exists
        else:
            total = 0
            for invoice_item in related_invoice_items:
                if invoice_item.unit_price < 0:
                    total -= invoice_item.quantity
                else:
                    total += invoice_item.quantity

        diff = new_quantity - total
        if diff == 0:
            return

        plan_component = resource.plan.components.get(component__type=component_type)
        details = cls.get_component_details(resource, plan_component)

        start = timezone.now()
        _, end = LimitPeriodProcessor.get_billing_period(
            offering_component.limit_period, start
        )

        # Create main invoice item for the difference
        final_unit_price = plan_component.price if diff > 0 else -plan_component.price

        invoice_models.InvoiceItem.objects.create(
            name=f"{cls.get_name(resource)} / {cls.get_component_name(plan_component)}",
            resource=resource,
            project=resource.project,
            unit_price=final_unit_price,
            unit=invoice_models.Units.QUANTITY,
            quantity=diff if diff > 0 else -diff,
            article_code=offering_component.article_code or resource.plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
            measured_unit=offering_component.measured_unit,
        )

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
    def calculate_discount_amount(
        cls,
        plan_component: marketplace_models.PlanComponent,
        quantity: int,
        unit_price: Decimal,
    ) -> tuple[Decimal, bool]:
        """
        Calculate discount amount based on quantity threshold and discount rate.

        Args:
            plan_component: Plan component with pricing and discount configuration
            quantity: Quantity being billed
            unit_price: Original unit price

        Returns:
            tuple: (discount_amount, discount_applies)
                - discount_amount: Total discount amount to subtract
                - discount_applies: Boolean indicating if discount threshold is met
        """
        # Check if discount is configured and threshold is met
        if (
            plan_component.discount_threshold is not None
            and plan_component.discount_rate is not None
            and quantity >= plan_component.discount_threshold
        ):
            # Calculate total discount amount
            total_before_discount = unit_price * quantity
            discount_amount = total_before_discount * (
                Decimal(plan_component.discount_rate) / Decimal(100)
            )

            logger.info(
                f"Discount applies for component '{plan_component.component.type}'. "
                f"Rate: {plan_component.discount_rate}%, Quantity: {quantity}, "
                f"Total before discount: {total_before_discount}, Discount amount: {discount_amount}"
            )
            return discount_amount, True

        return Decimal(0), False

    @classmethod
    def create_discount_invoice_item(
        cls,
        resource: marketplace_models.Resource,
        plan_component: marketplace_models.PlanComponent,
        invoice: invoice_models.Invoice,
        discount_amount: Decimal,
        quantity: int,
        start,
        end,
        component_name: str | None = None,
    ):
        """
        Create a separate invoice item for discount with negative unit price.

        Args:
            resource: Marketplace resource
            plan_component: Plan component with discount configuration
            invoice: Invoice to add the discount item to
            discount_amount: Total discount amount (positive value)
            quantity: Original quantity being discounted
            start: Billing period start
            end: Billing period end
            component_name: Optional custom component name for display
        """
        offering_component = plan_component.component

        details = cls.get_component_details(resource, plan_component)
        details["is_discount"] = True
        details["discount_threshold"] = plan_component.discount_threshold
        details["discount_rate"] = plan_component.discount_rate
        details["original_quantity"] = quantity
        details["discount_type"] = "volume_discount"

        component_display_name = component_name or cls.get_component_name(
            plan_component
        )
        discount_name = (
            f"{cls.get_name(resource)} / "
            f"{component_display_name} / "
            f"Volume Discount ({plan_component.discount_rate}%)"
        )

        invoice_models.InvoiceItem.objects.create(
            name=discount_name,
            resource=resource,
            project=resource.project,
            unit_price=-discount_amount,  # Negative to represent discount
            unit=invoice_models.Units.QUANTITY,
            quantity=1,  # Quantity of 1 since discount_amount is the total
            article_code=offering_component.article_code or resource.plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
            measured_unit="",  # No measured unit for discount items
        )

        logger.info(
            f"Created discount invoice item for resource '{resource.uuid}': "
            f"Discount amount: {discount_amount}, Rate: {plan_component.discount_rate}%"
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

        plan = plan_period.plan
        customer = resource.project.customer
        invoice, _ = MarketplaceBillingService.get_or_create_invoice(customer, date)

        # Try to find an existing invoice item for this component in the current period
        item = invoice.items.filter(
            resource=resource,
            details__offering_component_type=offering_component.type,
        ).first()

        try:
            plan_component = plan.components.get(component=offering_component)
        except ObjectDoesNotExist:
            logger.error(
                f"PlanComponent for component '{offering_component.type}' not found "
                f"in plan '{plan.name}' for resource '{resource.uuid}'. Cannot bill usage."
            )
            return

        converted_usage = cls.convert_quantity(
            usage_to_bill, resource.offering.type, offering_component.type
        )

        if item:
            item.quantity = converted_usage
            item.save(update_fields=["quantity"])
            logger.info(
                f"Updated invoice item {item.pk} for resource '{resource.uuid}'. New quantity: {item.quantity}"
            )
        else:
            # Create a new invoice item
            details = cls.get_component_details(resource, plan_component)
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
                project=resource.project,
                invoice=invoice,
                start=start,
                end=end,
                details=details,
                unit_price=plan_component.price,
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

    @classmethod
    def convert_quantity(cls, usage, offering_type, component_type: str):
        """
        Convert usage quantity for billing purposes.

        Base implementation returns usage as-is. Can be overridden in
        subclasses to apply component-specific conversion factors.

        Args:
            usage: Raw usage quantity
            component_type: Type of component being processed

        Returns:
            Converted quantity for billing
        """
        if offering_type == SLURM_OFFERING:
            return utils.convert_slurm_usage(usage, component_type)
        if offering_type == OPENSTACK_TENANT_OFFERING:
            if component_type in (STORAGE_TYPE, RAM_TYPE):
                return int(usage / 1024)
            return int(usage)
        return usage

    @classmethod
    def get_component_name(
        cls, plan_component: marketplace_models.PlanComponent
    ) -> str:
        """
        Get display name for a plan component.

        Args:
            plan_component: Plan component instance

        Returns:
            Component name for display purposes
        """
        if plan_component.component.offering.type == OPENSTACK_TENANT_OFFERING:
            component_type = plan_component.component.type
            if component_type == CORES_TYPE:
                return "CPU"
            elif component_type == RAM_TYPE:
                return "RAM"
            elif component_type == STORAGE_TYPE:
                return "storage"
            elif is_valid_volume_type_name(component_type):
                return f"{component_type.replace('gigabytes_', '')} storage"
        return plan_component.component.name

    @classmethod
    def connect(cls):
        """
        Connect the service to the billing system and Django signals.

        Registers the marketplace service with the billing system and
        connects signal handlers for resource lifecycle events and usage reporting.

        Signal connections:
        - Resource post_save: Handles billing for resource state changes
        - ComponentUsage post_save: Handles usage-based billing
        """
        signals.post_save.connect(
            cls.on_resource_post_save,
            sender=marketplace_models.Resource,
            dispatch_uid="%s.on_resource_post_save" % cls.__name__,
        )

        signals.post_save.connect(
            cls.update_invoice_when_usage_is_reported,
            sender=marketplace_models.ComponentUsage,
            dispatch_uid="waldur_mastermind.marketplace."
            "update_invoice_when_usage_is_reported_%s" % cls.__name__,
        )
