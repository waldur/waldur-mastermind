# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    #replaces = [('logging', '0002_index_alert_type')]

    dependencies = [
        ('logging', '0001_squashed_0003_emailhook_webhook'),
    ]

    operations = [
        migrations.AlterField(
            model_name='alert',
            name='alert_type',
            field=models.CharField(max_length=50, db_index=True),
            preserve_default=True,
        ),
    ]
