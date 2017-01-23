# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0006_offering'),
    ]

    operations = [
        migrations.AddField(
            model_name='offering',
            name='state',
            field=models.CharField(default='requested', max_length=30, choices=[('requested', 'Requested'), ('ok', 'OK'), ('terminated', 'Terminated')]),
        ),
    ]
