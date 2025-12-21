from django.db import models
from django.utils.translation import gettext_lazy as _


class Periods(models.IntegerChoices):
    TOTAL = 1, "Total"
    MONTH_1 = 2, "1 month"
    MONTH_3 = 3, "3 month"
    MONTH_12 = 4, "12 month"


class PaymentType(models.TextChoices):
    FIXED_PRICE = "fixed_price", "Fixed-price contract"
    MONTHLY_INVOICES = "invoices", "Monthly invoices"
    PAYMENT_GW_MONTHLY = "payment_gw_monthly", "Payment gateways (monthly)"


class InvoiceStates(models.TextChoices):
    PENDING = "pending", _("Pending")
    CREATED = "created", _("Created")
    PAID = "paid", _("Paid")
    CANCELED = "canceled", _("Canceled")


class MinimalConsumptionLogic(models.TextChoices):
    FIXED = "fixed", "Fixed"
    LINEAR = "linear", "Linear"
