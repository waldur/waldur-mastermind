# Generated manually to increase max_digits for default_tax_percent

import decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("structure", "0053_customer_max_service_accounts_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="default_tax_percent",
            field=models.DecimalField(
                decimal_places=2,
                default=decimal.Decimal(0),
                max_digits=5,
                validators=[
                    MinValueValidator(decimal.Decimal(0)),
                    MaxValueValidator(decimal.Decimal(200)),
                ],
            ),
        ),
    ]
