# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0007_migrate_hourly_price_to_daily'),
    ]

    operations = [
        migrations.AlterField(
            model_name='packagecomponent',
            name='type',
            field=models.CharField(max_length=50, choices=[('ram', 'RAM'), ('cores', 'Cores'), ('storage', 'Storage')]),
        ),
    ]
