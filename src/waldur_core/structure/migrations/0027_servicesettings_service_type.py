# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations
import waldur_core.structure.models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0026_add_error_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='servicesettings',
            name='service_type',
            field=models.CharField(max_length=255, default='', db_index=True, validators=[waldur_core.structure.models.validate_service_type]),
            preserve_default=True,
        )
    ]
