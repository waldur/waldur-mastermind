from calendar import monthrange
from decimal import ROUND_UP, Decimal

from django.db import migrations, models


class Units:
    PER_MONTH = 'month'
    PER_HALF_MONTH = 'half_month'
    PER_DAY = 'day'
    PER_HOUR = 'hour'
    QUANTITY = 'quantity'


def quantize_price(value):
    return value.quantize(Decimal('0.01'), rounding=ROUND_UP)


def get_full_hours(start, end):
    seconds_in_hour = 60 * 60
    full_hours, extra_seconds = divmod((end - start).total_seconds(), seconds_in_hour)
    if extra_seconds > 0:
        full_hours += 1

    return int(full_hours)


def get_full_days(start, end):
    seconds_in_day = 24 * 60 * 60
    full_days, extra_seconds = divmod((end - start).total_seconds(), seconds_in_day)
    if extra_seconds > 0:
        full_days += 1

    return int(full_days)


def get_quantity(item):
    month_days = monthrange(item.start.year, item.start.month)[1]

    if item.unit == Units.PER_HOUR:
        return get_full_hours(item.start, item.end)
    elif item.unit == Units.PER_DAY:
        return get_full_days(item.start, item.end)
    elif item.unit == Units.PER_HALF_MONTH:
        if (item.start.day == 1 and item.end.day == 15) or (
            item.start.day == 16 and item.end.day == month_days
        ):
            return 1
        elif item.start.day == 1 and item.end.day == month_days:
            return 2
        elif item.start.day == 1 and item.end.day > 15:
            return quantize_price(1 + (item.end.day - 15) / Decimal(month_days / 2))
        elif item.start.day < 16 and item.end.day == month_days:
            return quantize_price(1 + (16 - item.start.day) / Decimal(month_days / 2))
        else:
            return quantize_price(
                (item.end.day - item.start.day + 1) / Decimal(month_days / 2.0)
            )
    # By default PER_MONTH
    else:
        if item.start.day == 1 and item.end.day == month_days:
            return 1

        use_days = (item.end - item.start).days + 1
        return quantize_price(Decimal(use_days) / month_days)


def fill_quantity(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    for item in InvoiceItem.objects.all():
        if item.unit == Units.QUANTITY:
            continue
        if item.quantity:
            continue
        item.quantity = get_quantity(item)
        item.save(update_fields=['quantity'])


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0055_invoice_backend_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoiceitem',
            name='quantity',
            field=models.DecimalField(decimal_places=7, default=0, max_digits=22),
        ),
        migrations.RunPython(fill_quantity, reverse_code=migrations.RunPython.noop),
    ]
