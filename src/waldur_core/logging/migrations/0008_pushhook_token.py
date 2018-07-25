# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('logging', '0007_pushhook_unique_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='pushhook',
            name='token',
            field=models.CharField(max_length=255, unique=True, null=True),
        ),
    ]
