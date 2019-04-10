# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0076_plan_period_uuid_populate'),
    ]

    operations = [
        migrations.AlterField(
            model_name='resourceplanperiod',
            name='uuid',
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
