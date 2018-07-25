# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cost_tracking', '0012_fix_fields_length'),
    ]

    operations = [
        migrations.AlterField(
            model_name='defaultpricelistitem',
            name='item_type',
            field=models.CharField(max_length=255),
            preserve_default=True,
        ),
        migrations.AlterField(
            model_name='pricelistitem',
            name='item_type',
            field=models.CharField(max_length=255),
            preserve_default=True,
        ),
    ]
