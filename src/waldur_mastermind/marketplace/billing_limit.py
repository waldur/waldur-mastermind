import datetime
import logging
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace import billing_discount, utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_utils import (
    convert_quantity,
    get_component_details,
    get_component_name,
    get_invoice_item_name,
)
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    OrderTypes,
    ResourceStates,
)

logger = logging.getLogger(__name__)


class LimitPeriodProcessor:
    """
    Billing behavior varies by limit period for limit-based components:
    - MONTH: Billed monthly based on limits
    - QUARTERLY: Billed quarterly based on limits
    - ANNUAL: Billed annually based on limits
    - TOTAL: One-time billing for total limit amount
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
                cls._create_invoice_item(resource, plan_component, invoice, start, end)
            return

        if not cls._should_process_billing(limit_period, start, resource):
            # Skip billing for this period (e.g., non-quarterly month for a quarterly component)
            return

        # Use the appropriate billing period instead of the default monthly one
        if limit_period in (LimitPeriods.QUARTERLY, LimitPeriods.ANNUAL):
            billing_start, billing_end = cls._get_billing_period(
                limit_period, start, resource
            )
        else:
            # For MONTH, use the period provided by the caller.
            billing_start, billing_end = start, end

        cls._create_invoice_item(
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
            cls._create_invoice_item_for_total_limit(
                resource,
                invoice,
                component_type,
                new_quantity,
                offering_component,
            )
        else:
            cls._create_or_update_invoice_item(
                resource, invoice, component_type, new_quantity
            )

    # -- Private methods --

    @classmethod
    def _get_billing_period(
        cls,
        limit_period: str,
        date: datetime.date,
        resource: marketplace_models.Resource = None,
    ) -> tuple[datetime.datetime, datetime.datetime]:
        """
        Get the full billing period (start, end) for a given limit period
        containing the given date.

        For ANNUAL billing, the period is based on the resource's creation
        anniversary (12-month cycle from delivery date), not the calendar year.
        """
        if limit_period == LimitPeriods.QUARTERLY:
            return core_utils.get_quarter_start(date), core_utils.get_quarter_end(date)
        if limit_period == LimitPeriods.ANNUAL and resource:
            # Anniversary-based annual billing: 12-month cycle from resource creation
            anniversary_this_year = resource.created.replace(year=date.year)
            if anniversary_this_year > date:
                start = resource.created.replace(year=date.year - 1)
            else:
                start = anniversary_this_year
            end = start.replace(year=start.year + 1) - datetime.timedelta(seconds=1)
            return start, end
        # Default to monthly boundaries for MONTH and other recurring limit types.
        return core_utils.month_start(date), core_utils.month_end(date)

    @classmethod
    def _should_process_billing(
        cls,
        limit_period: str,
        date: datetime.date,
        resource: marketplace_models.Resource = None,
    ) -> bool:
        """
        Check if billing should be processed for the given date based on the limit period.

        For ANNUAL billing, billing triggers on the resource's creation anniversary month,
        not on a fixed calendar month.
        """
        if limit_period == LimitPeriods.QUARTERLY:
            # Quarterly billing should only happen in the first month of each quarter:
            # January (Q1), April (Q2), July (Q3), October (Q4)
            return date.month in [1, 4, 7, 10]
        if limit_period == LimitPeriods.ANNUAL:
            # Annual billing triggers on the resource's creation anniversary month.
            return resource is not None and date.month == resource.created.month
        # MONTH, TOTAL are processed every month.
        return True

    @classmethod
    def _create_invoice_item_for_total_limit(
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
        # Discount items produced at invoice finalization carry the same
        # offering_component_type; they are bookkeeping lines, not billed
        # quantity, so they must not participate in the already-billed total.
        # has_key (not details__is_discount=True) because exclude() on a JSON
        # value would also drop rows where the key is absent entirely.
        related_invoice_items = invoice_models.InvoiceItem.objects.filter(
            resource=resource,
            details__offering_component_type=component_type,
        ).exclude(details__has_key="is_discount")
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

        # resource.limits is a JSONField, so a fractional limit arrives as a
        # float, while the billed total is a Decimal summed from
        # InvoiceItem.quantity. float - Decimal raises TypeError, which
        # surfaced as a bare HTTP 500 from set_limits once the resource had
        # billing history. Coerce via str() so 0.1 becomes Decimal("0.1")
        # rather than its binary expansion.
        diff = Decimal(str(new_quantity)) - Decimal(total)
        if diff == 0:
            return

        try:
            plan_component = resource.plan.components.get(
                component__type=component_type
            )
        except ObjectDoesNotExist:
            logger.warning(
                "Skipping processing of invoice item %s because "
                "plan component is not defined.",
                component_type,
            )
            return
        details = get_component_details(resource, plan_component)

        # Record the signed limit delta feeding the volume discount aggregation
        # (billing_discount): summing this item's value with the resource's
        # earlier TOTAL items yields the current total limit, so tier formulas
        # evaluate on the net limit after increases and decreases alike.
        # Compensation items (negative delta) can never produce a discount line
        # themselves — billing_discount._create_discount_item skips items whose
        # discount amount is not positive.
        details[billing_discount.DISCOUNT_USAGE_KEY] = float(diff)

        start = timezone.now()
        _, end = cls._get_billing_period(
            offering_component.limit_period, start, resource
        )

        # Create main invoice item for the difference
        final_unit_price = plan_component.price if diff > 0 else -plan_component.price

        invoice_models.InvoiceItem.objects.create(
            name=f"{get_invoice_item_name(resource)} / {get_component_name(plan_component)}",
            resource=resource,
            plan_component=plan_component,
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
    def _create_or_update_invoice_item(
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
            cls._update_invoice_item(resource, component_type, invoice, quantity)
        else:
            now = timezone.now()
            try:
                plan_component = resource.plan.components.get(
                    component__type=component_type
                )
                # Get the offering component to determine appropriate period end
                offering_component = plan_component.component

                start, end = cls._get_billing_period(
                    offering_component.limit_period, now, resource
                )

            except ObjectDoesNotExist:
                logger.warning(
                    "Skipping processing of invoice item %s because "
                    "plan component is not defined.",
                    component_type,
                )
                return
            else:
                # For quarterly/annual components, the invoice item may live on
                # the billing period's first month's invoice (e.g., January for Q1),
                # not the current month's invoice. Check for it there.
                if offering_component.limit_period in (
                    LimitPeriods.QUARTERLY,
                    LimitPeriods.ANNUAL,
                ):
                    period_invoice = invoice_models.Invoice.objects.filter(
                        customer=resource.project.customer,
                        year=start.year,
                        month=start.month,
                    ).first()
                    if (
                        period_invoice
                        and period_invoice != invoice
                        and invoice_models.InvoiceItem.objects.filter(
                            resource=resource,
                            details__offering_component_type=component_type,
                            invoice=period_invoice,
                        ).exists()
                    ):
                        cls._update_invoice_item(
                            resource, component_type, period_invoice, quantity
                        )
                        return

                cls._create_invoice_item(resource, plan_component, invoice, start, end)

    @classmethod
    def _update_invoice_item(
        cls,
        resource: marketplace_models.Resource,
        component_type: str,
        invoice: invoice_models.Invoice,
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
        new_quantity = convert_quantity(
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
        _, period_end = cls._get_billing_period(
            offering_component.limit_period, timezone.now(), resource
        )

        new_period = utils.serialize_resource_limit_period(
            new_start, period_end, new_quantity
        )
        resource_limit_periods.extend([old_period, new_period])
        plan_component = resource.plan.components.get(component__type=component_type)

        unit = plan_component.plan.unit
        if unit == invoice_models.InvoiceItem.Units.PER_DAY:
            # For PER_DAY, _get_total_quantity correctly returns value * days,
            # so summing sub-periods naturally preserves proration.
            invoice_item.quantity = sum(
                cls._get_total_quantity(
                    unit,
                    period["quantity"],
                    parse_datetime(period["start"]),
                    parse_datetime(period["end"]),
                )
                for period in resource_limit_periods
            )
        else:
            # For non-day units (PER_MONTH, PER_QUARTER, etc.),
            # _get_total_quantity returns the raw value regardless of period
            # length. Summing raw values across sub-periods would add old + new
            # limits together instead of prorating. We must weight each
            # sub-period's quantity by its fraction of the total billing period.
            total_days = get_full_days(invoice_item.start, invoice_item.end)
            if total_days > 0:
                invoice_item.quantity = sum(
                    period["quantity"]
                    * get_full_days(
                        parse_datetime(period["start"]),
                        parse_datetime(period["end"]),
                    )
                    / total_days
                    for period in resource_limit_periods
                )
            else:
                invoice_item.quantity = 0

        # Keep the volume-discount basis in sync with the limit change. The
        # basis is the period-weighted average limit: each sub-period's limit
        # weighted by its share of the billing period, matching what the
        # customer is actually charged for (see billing_discount). Only refresh
        # items that already participate in discounting; when the period length
        # is zero the previous basis is kept, mirroring the quantity fallback.
        if billing_discount.DISCOUNT_USAGE_KEY in invoice_item.details:
            total_days = get_full_days(invoice_item.start, invoice_item.end)
            if total_days > 0:
                weighted_limit = sum(
                    period["quantity"]
                    * get_full_days(
                        parse_datetime(period["start"]),
                        parse_datetime(period["end"]),
                    )
                    / total_days
                    for period in resource_limit_periods
                )
                invoice_item.details[billing_discount.DISCOUNT_USAGE_KEY] = float(
                    weighted_limit
                )

        invoice_item.save(update_fields=["details", "quantity"])

    @classmethod
    def _create_invoice_item(
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
        details = get_component_details(source, plan_component)
        quantity = convert_quantity(
            limit, source.offering.type, offering_component.type
        )
        details["resource_limit_periods"] = [
            utils.serialize_resource_limit_period(start, end, quantity)
        ]

        unit = plan_component.plan.unit
        if (
            offering_component.billing_type == BillingTypes.LIMIT
            and offering_component.limit_period == LimitPeriods.TOTAL
        ):
            # TOTAL is a one-time charge: use the raw limit, no day multiplication
            total_quantity = quantity
            unit = invoice_models.Units.QUANTITY
            details["limit_period"] = LimitPeriods.TOTAL
        else:
            total_quantity = cls._get_total_quantity(
                plan_component.plan.unit, quantity, start, end
            )

        # Record the volume that feeds the org-aggregated volume discount: the
        # raw component quantity (the limit), while the discount later applies
        # to the actual line charge (price x duration-multiplied total_quantity).
        # The discount is materialized at invoice finalization (billing_discount).
        details[billing_discount.DISCOUNT_USAGE_KEY] = float(quantity)

        invoice_models.InvoiceItem.objects.create(
            name=f"{get_invoice_item_name(source)} / {get_component_name(plan_component)}",
            resource=source,
            plan_component=plan_component,
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
    def _get_total_quantity(cls, unit, value, start, end):
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
