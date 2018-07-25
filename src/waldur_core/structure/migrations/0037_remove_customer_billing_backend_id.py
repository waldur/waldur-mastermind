# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0036_add_vat_fields'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='customer',
            name='billing_backend_id',
        ),
    ]
