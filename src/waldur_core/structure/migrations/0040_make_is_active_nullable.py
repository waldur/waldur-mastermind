# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0039_remove_permission_groups'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customerpermission',
            name='is_active',
            field=models.NullBooleanField(default=True, db_index=True),
        ),
        migrations.AlterField(
            model_name='projectpermission',
            name='is_active',
            field=models.NullBooleanField(default=True, db_index=True),
        ),
    ]
