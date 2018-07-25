# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('logging', '0006_pushhook_systemnotification'),
    ]

    operations = [
        migrations.RenameField(
            model_name='pushhook',
            old_name='registration_token',
            new_name='device_id',
        ),
        migrations.AlterField(
            model_name='pushhook',
            name='device_id',
            field=models.CharField(max_length=255, unique=True, null=True),
        ),
        migrations.AlterUniqueTogether(
            name='pushhook',
            unique_together=set([('user', 'device_id', 'type')]),
        ),
    ]
