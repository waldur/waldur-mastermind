class Units:
    PER_MONTH = "month"
    PER_QUARTER = "quarter"
    PER_HALF_MONTH = "half_month"
    PER_DAY = "day"
    PER_HOUR = "hour"
    QUANTITY = "quantity"

    CHOICES = (
        (PER_MONTH, "Per month"),
        (PER_QUARTER, "Per quarter"),
        (PER_HALF_MONTH, "Per half month"),
        (PER_DAY, "Per day"),
        (PER_HOUR, "Per hour"),
        (QUANTITY, "Quantity"),
    )
