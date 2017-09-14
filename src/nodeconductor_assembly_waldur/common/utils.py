from decimal import Decimal, ROUND_UP


def quantize_price(value):
    """
    Returns value rounded up to 2 places after the decimal point.
    :rtype: Decimal
    """
    return value.quantize(Decimal('0.01'), rounding=ROUND_UP)
