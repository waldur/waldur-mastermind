# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0004_packagetemplate_category'),
    ]

    operations = [
        migrations.AlterField(
            model_name='openstackpackage',
            name='service_settings',
            field=models.ForeignKey(related_name='+', on_delete=django.db.models.deletion.SET_NULL, to='structure.ServiceSettings', null=True),
        ),
    ]
