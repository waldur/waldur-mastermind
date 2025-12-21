from django.db import models


class Units(models.TextChoices):
    """Billing unit types for pricing."""

    PER_MONTH = "month", "Per month"
    PER_QUARTER = "quarter", "Per quarter"
    PER_HALF_MONTH = "half_month", "Per half month"
    PER_DAY = "day", "Per day"
    PER_HOUR = "hour", "Per hour"
    QUANTITY = "quantity", "Quantity"
