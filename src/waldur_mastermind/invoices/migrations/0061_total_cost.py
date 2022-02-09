import decimal

from django.db import migrations, models


def quantize_price(value):
    return value.quantize(decimal.Decimal('0.01'), rounding=decimal.ROUND_UP)


def fill_total_cost(apps, schema_editor):
    Invoice = apps.get_model('invoices', 'Invoice')
    for invoice in Invoice.objects.all():
        price = quantize_price(
            decimal.Decimal(
                sum(
                    quantize_price(item.unit_price * decimal.Decimal(item.quantity))
                    for item in invoice.items.all()
                )
            )
        )
        invoice.total_cost = price * (1 + invoice.tax_percent / 100)
        invoice.save(update_fields=['total_cost'])


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0060_alter_paymentprofile_is_active'),
    ]

    operations = [
        migrations.RenameField(
            model_name='invoice', old_name='current_cost', new_name='total_cost',
        ),
        migrations.AlterField(
            model_name='invoice',
            name='total_cost',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                editable=False,
                help_text='Cached value for total cost.',
                max_digits=10,
            ),
        ),
        migrations.RunPython(fill_total_cost, reverse_code=migrations.RunPython.noop),
    ]
