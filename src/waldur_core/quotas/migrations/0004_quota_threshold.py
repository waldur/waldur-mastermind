# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('quotas', '0003_index_quota_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='quota',
            name='threshold',
            field=models.FloatField(default=0, validators=[django.core.validators.MinValueValidator(0)]),
        ),
    ]
