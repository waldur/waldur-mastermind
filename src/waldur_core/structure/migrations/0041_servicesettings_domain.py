# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0040_make_is_active_nullable'),
    ]

    operations = [
        migrations.AddField(
            model_name='servicesettings',
            name='domain',
            field=models.CharField(max_length=200, null=True, blank=True),
        ),
    ]
