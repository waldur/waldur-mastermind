# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cost_tracking', '0013_remove_item_type_choices'),
    ]

    operations = [
        migrations.AlterField(
            model_name='defaultpricelistitem',
            name='value',
            field=models.DecimalField(default=0, verbose_name=b'Hourly rate', max_digits=11, decimal_places=5),
            preserve_default=True,
        ),
        migrations.AlterField(
            model_name='pricelistitem',
            name='value',
            field=models.DecimalField(default=0, verbose_name=b'Hourly rate', max_digits=11, decimal_places=5),
            preserve_default=True,
        ),
    ]
