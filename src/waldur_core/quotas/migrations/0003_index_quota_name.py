# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('quotas', '0002_make_quota_scope_nullable'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quota',
            name='name',
            field=models.CharField(max_length=150, db_index=True),
            preserve_default=True,
        ),
    ]
