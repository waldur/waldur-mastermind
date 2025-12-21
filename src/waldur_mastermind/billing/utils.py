import decimal

from django.db import models
from django.db.models import Case, F, Sum, When
from django.db.models.functions import Ceil, Extract, Least
from django.utils import timezone

from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices.models import InvoiceItem

SECONDS_IN_HOUR = 60 * 60
"""Number of seconds in one hour."""

SECONDS_IN_DAY = 24 * 60 * 60
"""Number of seconds in one day."""


def get_current_expression():
    """
    Generate a Django ORM expression to calculate the current quantity for invoice items.

    This function creates a database expression that calculates the appropriate quantity
    based on the billing unit (per hour or per day) and the time elapsed from start to
    the current time or end time, whichever is earlier.

    Returns:
        Case: A Django Case expression that returns:
            - For PER_HOUR units: quantity calculated based on hours elapsed (rounded up)
            - For PER_DAY units: quantity calculated based on days elapsed (rounded up)
            - For other units: the original quantity field value

    Note:
        The calculation uses the ceiling function to round up partial time units,
        ensuring that any partial hour or day is billed as a full unit.
    """
    effective_end = Least("end", timezone.now())

    duration_in_seconds = Extract(effective_end - F("start"), "epoch")

    return Case(
        When(
            unit=InvoiceItem.Units.PER_HOUR,
            then=Ceil(duration_in_seconds / decimal.Decimal(SECONDS_IN_HOUR)),
        ),
        When(
            unit=InvoiceItem.Units.PER_DAY,
            then=Ceil(duration_in_seconds / decimal.Decimal(SECONDS_IN_DAY)),
        ),
        default=F("quantity"),
        output_field=models.DecimalField(),
    )


def aggregate_invoice_items_sum(
    qs: models.QuerySet[InvoiceItem], current: bool, tax: bool
) -> decimal.Decimal:
    """
    Calculate the total sum of invoice items with optional current quantity calculation and tax.

    This function aggregates invoice items by calculating the total cost based on unit prices
    and quantities. It supports both historical billing (using stored quantities) and current
    billing (calculating quantities based on elapsed time).

    Args:
        qs: QuerySet of InvoiceItem objects to aggregate
        current: If True, calculate quantities based on current time elapsed from start.
                If False, use the stored quantity values.
        tax: If True, include tax in the calculation using the invoice's tax_percent.
             If False, return the pre-tax total.

    Returns:
        decimal.Decimal: The total sum of all invoice items, quantized to standard price precision.
                        Returns 0.00 if no items exist or the sum is None.

    Note:
        - When current=True, quantities are recalculated based on time elapsed and billing units
        - Tax is calculated as a percentage of the total (tax_percent / 100)
        - The result is quantized using the standard price quantization function
    """
    if current:
        qs = qs.annotate(calculated_quantity=get_current_expression())
    else:
        qs = qs.annotate(calculated_quantity=F("quantity"))

    qs = qs.annotate(calculated_total=F("unit_price") * F("calculated_quantity"))

    if tax:
        sum_expression = Sum(F("calculated_total") * F("invoice__tax_percent") / 100)
    else:
        sum_expression = Sum(F("calculated_total"))

    aggregation = qs.aggregate(total=sum_expression)
    return quantize_price(aggregation["total"] or decimal.Decimal("0.00"))
