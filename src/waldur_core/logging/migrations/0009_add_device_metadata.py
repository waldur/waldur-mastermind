# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('logging', '0008_pushhook_token'),
    ]

    operations = [
        migrations.AddField(
            model_name='pushhook',
            name='device_manufacturer',
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name='pushhook',
            name='device_model',
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
    ]
