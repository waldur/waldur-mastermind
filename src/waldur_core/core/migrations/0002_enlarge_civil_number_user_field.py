# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_squashed_0003_ssh_key_name_length_changed'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='civil_number',
            field=models.CharField(null=True, default=None, max_length=50, blank=True, unique=True, verbose_name='civil number'),
        ),
    ]
