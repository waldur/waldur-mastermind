# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0003_add_meta_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='packagetemplate',
            name='category',
            field=models.CharField(default='small', max_length=10, choices=[('small', 'Small'), ('medium', 'Medium'), ('large', 'Large')]),
        ),
    ]
