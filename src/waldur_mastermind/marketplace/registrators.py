import logging
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q, signals
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.invoices.registrators import RegistrationManager
from waldur_mastermind.invoices.utils import (
    get_current_month_end,
    get_full_days,
)
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    LimitPeriods,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import ComponentUsage
from waldur_mastermind.promotions import models as promotions_models

logger = logging.getLogger(__name__)


class MarketplaceRegistrator(registrators.BaseRegistrator):
    """
    Marketplace invoice registrator handling billing for different component types.

    This registrator manages invoice generation for marketplace resources based on
    their offering components' billing types:

    - FIXED: Fixed-price components billed monthly with prorated quantities
    - USAGE: Usage-based components billed based on reported usage
    - LIMIT: Limit-based components billed based on user-specified limits
    - ONE_TIME: One-time components billed once during resource creation
    - ON_PLAN_SWITCH: Components billed when switching between plans

    The registrator handles different billing scenarios:
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

    plugin_name = BASIC_OFFERING

    def _find_item(self, source: marketplace_models.Resource, now):
        """
        Find invoice items for a marketplace resource by source and date.

        Searches for existing invoice items for the given resource within the
        specified billing period. Used to avoid duplicate billing and to update
        existing items when needed.

        Args:
            source: Marketplace resource that was purchased by customer
            now: Date of invoice with invoice items

        Returns:
            List of invoice items for the resource in the specified period
        """

        return list(
            invoice_models.InvoiceItem.objects.filter(
                resource=source,
                invoice__customer=self.get_customer(source),
                invoice__state=invoice_models.Invoice.States.PENDING,
                invoice__year=now.year,
                invoice__month=now.month,
                end=core_utils.month_end(now),
            )
        )

    def get_sources(self, customer):
        """
        Get billable marketplace resources for a customer.

        Returns all marketplace resources for the customer that are in billable
        states (excluding CREATING and TERMINATED states). These resources will
        be processed for invoice generation.

        Args:
            customer: Customer object to get resources for

        Returns:
            QuerySet of marketplace resources ready for billing
        """
        return (
            marketplace_models.Resource.objects.filter(
                offering__type=self.plugin_name,
                project__customer=customer,
            )
            .exclude(state__in=[ResourceStates.CREATING, ResourceStates.TERMINATED])
            .distinct()
        )

    def get_customer(self, source):
        """
        Get customer for a marketplace resource.

        Args:
            source: Marketplace resource

        Returns:
            Customer object that owns the resource
        """
        return source.project.customer

    def _create_item(
        self,
        source: marketplace_models.Resource,
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
        resource = source
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
                # Avoid creating invoice item for limit-based components
                # if limit period is total and resource is not being created
                if offering_component.limit_period == LimitPeriods.TOTAL:
                    if order_type == OrderTypes.CREATE:
                        self.create_component_item(
                            source, plan_component, invoice, start, end
                        )
                        continue
                    else:
                        continue

                # For quarterly components, only process billing in quarterly months
                if offering_component.limit_period == LimitPeriods.QUARTERLY:
                    if not self.should_process_quarterly_billing(start):
                        # Skip quarterly billing in non-quarterly months
                        continue
                    # Use quarterly billing period instead of monthly
                    quarterly_start, quarterly_end = self.get_quarterly_billing_period(
                        start
                    )
                    self.create_component_item(
                        source, plan_component, invoice, quarterly_start, quarterly_end
                    )
                    continue

                # Process monthly, annual, and other limit periods normally
                self.create_component_item(source, plan_component, invoice, start, end)
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
                name = f"{self.get_name(resource)} / {self.get_component_name(plan_component)}"
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

    def get_name(self, resource: marketplace_models.Resource):
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
        Create invoice item for limit-based components.

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
        quantity = cls.convert_quantity(limit, offering_component.type)
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
            name=f"{RegistrationManager.get_name(source)} / {cls.get_component_name(plan_component)}",
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

    @classmethod
    def update_component_item(
        cls, source: marketplace_models.Resource, component_type, invoice, new_quantity
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
        if not source.plan:
            return

        invoice_item = invoice_models.InvoiceItem.objects.get(
            resource=source,
            details__offering_component_type=component_type,
            details__plan_uuid=str(source.plan.uuid),
            invoice=invoice,
            unit_price__gte=0,  # exclude compensation items
        )
        resource_limit_periods = invoice_item.details["resource_limit_periods"]
        old_period = resource_limit_periods.pop()
        old_quantity = int(old_period["quantity"])
        old_start = parse_datetime(old_period["start"])
        today = timezone.now()
        new_quantity = cls.convert_quantity(new_quantity, component_type)
        if old_quantity == new_quantity:
            # Skip update if limit is the same
            return
        if old_quantity > new_quantity:
            old_end = today.replace(hour=23, minute=59, second=59)
            new_start = old_end + timedelta(seconds=1)
        else:
            new_start = today.replace(hour=0, minute=0, second=0)
            old_end = new_start - timedelta(seconds=1)
        old_period = utils.serialize_resource_limit_period(
            old_start, old_end, old_quantity
        )
        # Get the offering component to determine appropriate period end
        offering_component = source.offering.components.get(type=component_type)
        period_end = cls.get_period_end_for_limit_period(
            offering_component.limit_period
        )

        new_period = utils.serialize_resource_limit_period(
            new_start, period_end, new_quantity
        )
        resource_limit_periods.extend([old_period, new_period])
        plan_component = source.plan.components.get(component__type=component_type)
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
        source: marketplace_models.Resource,
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
        if not source.plan:
            logger.warning(
                "Skipping processing of invoice item %s because "
                "billing is not enabled for resource.",
                component_type,
            )
            return
        if invoice_models.InvoiceItem.objects.filter(
            resource=source,
            details__offering_component_type=component_type,
            invoice=invoice,
        ).exists():
            cls.update_component_item(source, component_type, invoice, quantity)
        else:
            start = timezone.now()
            try:
                plan_component = source.plan.components.get(
                    component__type=component_type
                )
                # Get the offering component to determine appropriate period end
                offering_component = plan_component.component

                # Handle quarterly components with proper billing periods
                if offering_component.limit_period == LimitPeriods.QUARTERLY:
                    quarterly_start, quarterly_end = cls.get_quarterly_billing_period(
                        start
                    )
                    end = quarterly_end
                    start = quarterly_start
                else:
                    end = cls.get_period_end_for_limit_period(
                        offering_component.limit_period
                    )

            except ObjectDoesNotExist:
                logger.warning(
                    "Skipping processing of invoice item %s because "
                    "plan component is not defined.",
                    component_type,
                )
                return
            else:
                cls.create_component_item(source, plan_component, invoice, start, end)

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
        if resource.offering.type != cls.plugin_name:
            return

        if created:
            return

        resource_tracker = resource.tracker
        instance_tracker = instance.tracker

        if (
            resource.state == ResourceStates.OK
            and resource_tracker.previous("state") == ResourceStates.CREATING
        ):
            cls.create_discounted_resource(sender, instance, created)
            registrators.RegistrationManager.register(
                resource, timezone.now(), order_type=OrderTypes.CREATE
            )

        if (
            resource.state == ResourceStates.TERMINATED
            and instance_tracker.previous("state") == ResourceStates.TERMINATING
        ):
            registrators.RegistrationManager.terminate(resource, timezone.now())

        if resource.state != ResourceStates.CREATING and resource_tracker.has_changed(
            "plan_id"
        ):
            registrators.RegistrationManager.terminate(resource, timezone.now())
            registrators.RegistrationManager.register(
                resource, timezone.now(), order_type=OrderTypes.UPDATE
            )

        if resource.state != ResourceStates.CREATING and resource_tracker.has_changed(
            "limits"
        ):
            today = timezone.now()
            invoice, _ = registrators.RegistrationManager.get_or_create_invoice(
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
                offering_component = resource.offering.components.get(
                    type=component_type
                )
                if (
                    offering_component.billing_type == BillingTypes.LIMIT
                    and offering_component.limit_period == LimitPeriods.TOTAL
                ):
                    cls.create_invoice_item_for_total_limit(
                        resource,
                        invoice,
                        component_type,
                        new_quantity,
                        offering_component,
                    )
                else:
                    cls.create_or_update_component_item(
                        resource, invoice, component_type, new_quantity
                    )

    @classmethod
    def create_invoice_item_for_total_limit(
        cls,
        resource: marketplace_models.Resource,
        invoice: invoice_models.Invoice,
        component_type,
        new_quantity,
        offering_component,
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
        end = cls.get_period_end_for_limit_period(offering_component.limit_period)
        invoice_models.InvoiceItem.objects.create(
            name=f"{RegistrationManager.get_name(resource)} / {cls.get_component_name(plan_component)}",
            resource=resource,
            project=resource.project,
            unit_price=plan_component.price if diff > 0 else -plan_component.price,
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
    def update_invoice_when_usage_is_reported(
        cls, sender, instance: ComponentUsage, created=False, **kwargs
    ):
        """
        Handle usage-based billing when component usage is reported.

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

        if not created and not component_usage.tracker.has_changed("usage"):
            return

        if resource.offering.type != cls.plugin_name:
            return

        offering_component = component_usage.component
        # It is allowed to report usage for limit-based components but they are ignored in invoicing
        if offering_component.billing_type != BillingTypes.USAGE:
            return

        logger.info(
            "Processing component usage %s, amount: %s",
            component_usage,
            component_usage.usage,
        )

        plan_period = component_usage.plan_period
        if not plan_period:
            logger.warning(
                "Skipping processing of component usage %s (ID %s) because "
                "plan period is not defined.",
                component_usage,
                component_usage.id,
            )
            return
        plan = plan_period.plan

        item = utils.get_invoice_item_for_component_usage(component_usage)
        if item:
            logger.info(
                "Invoice item %s already exists, updating quantity from %s",
                item,
                item.quantity,
            )
            item.quantity = cls.convert_quantity(
                component_usage.usage, offering_component.type
            )
            item.save()
            logger.info("The %s quantity is set to %s", item, item.quantity)
        else:
            logger.info("No invoice item for the %s, creating one", component_usage)
            try:
                plan_component = plan.components.get(component=offering_component)
            except ObjectDoesNotExist:
                logger.warning(
                    "Skipping processing of component usage %s (ID %s) because "
                    "plan component is not defined.",
                    component_usage,
                    component_usage.id,
                )
                return
            customer = resource.project.customer
            invoice, _ = registrators.RegistrationManager.get_or_create_invoice(
                customer, component_usage.date
            )

            details = cls.get_component_details(resource, plan_component)

            month_start = core_utils.month_start(component_usage.date)
            month_end = core_utils.month_end(component_usage.date)

            start = (
                month_start
                if not component_usage.plan_period.start
                else max(component_usage.plan_period.start, month_start)
            )
            end = (
                month_end
                if not component_usage.plan_period.end
                else min(component_usage.plan_period.end, month_end)
            )

            logger.info("About to create an invoice item for the %s", component_usage)

            invoice_item = invoice_models.InvoiceItem.objects.create(
                resource=resource,
                project=resource.project,
                invoice=invoice,
                start=start,
                end=end,
                details=details,
                unit_price=plan_component.price,
                quantity=cls.convert_quantity(
                    component_usage.usage, offering_component.type
                ),
                unit=common_mixins.UnitPriceMixin.Units.QUANTITY,
                measured_unit=offering_component.measured_unit,
                article_code=offering_component.article_code or plan.article_code,
                name=resource.name + " / " + offering_component.name,
            )

            logger.info(
                "Invoice item has been successfully created for the %s, quantity %s",
                component_usage,
                invoice_item.quantity,
            )

    @classmethod
    def get_period_end_for_limit_period(cls, limit_period):
        """
        Get appropriate period end based on limit period.

        Args:
            limit_period: The limit period (MONTH, QUARTERLY, ANNUAL, TOTAL)

        Returns:
            Period end datetime for the given limit period
        """
        if limit_period == LimitPeriods.QUARTERLY:
            return core_utils.get_current_quarter_end()
        else:
            # Default to monthly for MONTH, ANNUAL, and TOTAL
            # ANNUAL and TOTAL periods still use monthly billing boundaries
            # but track usage over longer periods
            return get_current_month_end()

    @classmethod
    def should_process_quarterly_billing(cls, date):
        """
        Check if quarterly billing should be processed for the given date.

        Quarterly billing should only happen in the first month of each quarter:
        - January (Q1), April (Q2), July (Q3), October (Q4)

        Args:
            date: Date to check (datetime object)

        Returns:
            bool: True if quarterly billing should be processed
        """
        return date.month in [1, 4, 7, 10]

    @classmethod
    def get_quarterly_billing_period(cls, date):
        """
        Get the quarterly billing period for the given date.

        Args:
            date: Date within the quarter (datetime object)

        Returns:
            tuple: (quarter_start, quarter_end) datetime objects
        """
        quarter_start = core_utils.get_quarter_start(date)
        quarter_end = core_utils.get_quarter_end(date)
        return quarter_start, quarter_end

    @classmethod
    def convert_quantity(cls, usage, component_type: str):
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
        return usage

    @classmethod
    def get_component_name(cls, plan_component):
        """
        Get display name for a plan component.

        Args:
            plan_component: Plan component instance

        Returns:
            Component name for display purposes
        """
        return plan_component.component.name

    @classmethod
    def connect(cls):
        """
        Connect the registrator to the billing system and Django signals.

        Registers the marketplace registrator with the billing system and
        connects signal handlers for resource lifecycle events and usage reporting.

        Signal connections:
        - Resource post_save: Handles billing for resource state changes
        - ComponentUsage post_save: Handles usage-based billing
        """
        registrators.RegistrationManager.add_registrator(cls.plugin_name, cls)

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
