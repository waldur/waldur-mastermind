# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0006_offering'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='offering',
            options={'ordering': ['-created']},
        ),
        migrations.RemoveField(
            model_name='offering',
            name='type_label',
        ),
    ]
