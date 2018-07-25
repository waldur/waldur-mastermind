# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0025_add_zabbix_to_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='servicesettings',
            name='error_message',
            field=models.TextField(blank=True),
            preserve_default=True,
        ),
    ]
