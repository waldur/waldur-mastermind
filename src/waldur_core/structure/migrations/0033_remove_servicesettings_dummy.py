# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0032_make_options_optional'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='servicesettings',
            name='dummy',
        ),
    ]
