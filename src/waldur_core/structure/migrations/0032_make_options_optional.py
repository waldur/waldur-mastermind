# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0031_add_options_default'),
    ]

    operations = [
        migrations.AlterField(
            model_name='servicesettings',
            name='options',
            field=waldur_core.core.fields.JSONField(default={}, help_text='Extra options', blank=True),
            preserve_default=True,
        ),
    ]
