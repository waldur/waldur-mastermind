# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cost_tracking', '0024_refactor_price_estimate'),
    ]

    operations = [
        migrations.AlterField(
            model_name='defaultpricelistitem',
            name='value',
            field=models.DecimalField(default=0, verbose_name='Hourly rate', max_digits=13, decimal_places=7),
        ),
        migrations.AlterField(
            model_name='pricelistitem',
            name='value',
            field=models.DecimalField(default=0, verbose_name='Hourly rate', max_digits=13, decimal_places=7),
        ),
    ]
