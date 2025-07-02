import decimal

from django.db import models
from django.db.models import (
    Case,
    ExpressionWrapper,
    F,
    Sum,
    When,
)
from django.db.models.functions import Ceil, Least
from django.utils import timezone

from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices.models import InvoiceItem

SECONDS_IN_HOUR = 60 * 60

SECONDS_IN_DAY = 24 * 60 * 60


def get_current_expression():
    effective_end = Least("end", timezone.now())

    duration_in_seconds = ExpressionWrapper(
        models.functions.Extract(effective_end - F("start"), "epoch"),
        output_field=models.DecimalField(),
    )

    quantity_per_hour = Ceil(duration_in_seconds / decimal.Decimal(SECONDS_IN_HOUR))
    quantity_per_day = Ceil(duration_in_seconds / decimal.Decimal(SECONDS_IN_DAY))

    return Case(
        When(unit=InvoiceItem.Units.PER_HOUR, then=quantity_per_hour),
        When(unit=InvoiceItem.Units.PER_DAY, then=quantity_per_day),
        default=F("quantity"),
        output_field=models.DecimalField(),
    )


def aggregate_invoice_items_sum(
    qs: models.QuerySet[InvoiceItem], current: bool, tax: bool
) -> decimal.Decimal:
    quantity_expression = current and get_current_expression() or F("quantity")
    total_expression = F("unit_price") * quantity_expression
    tax_expression = (
        total_expression * F("invoice__tax_percent") / 100 if tax else total_expression
    )
    sum_expression = Sum(
        ExpressionWrapper(tax_expression, output_field=models.DecimalField())
    )

    return quantize_price(
        qs.aggregate(total=sum_expression)["total"] or decimal.Decimal("0.00")
    )
