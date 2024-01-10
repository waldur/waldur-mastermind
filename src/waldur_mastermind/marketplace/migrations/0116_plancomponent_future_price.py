from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0115_consumer_reviewed_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="plancomponent",
            name="future_price",
            field=models.DecimalField(
                decimal_places=10,
                max_digits=22,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                verbose_name="Price per unit for future month.",
                null=True,
            ),
        ),
    ]
