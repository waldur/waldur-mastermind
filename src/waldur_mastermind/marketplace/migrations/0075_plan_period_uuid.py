# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0074_attribute_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='resourceplanperiod',
            name='uuid',
            field=models.UUIDField(null=True),
        ),
    ]
