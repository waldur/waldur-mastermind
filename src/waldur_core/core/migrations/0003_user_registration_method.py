# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_enlarge_civil_number_user_field'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='registration_method',
            field=models.CharField(default='default', help_text='Indicates what registration method were used.', max_length=50, verbose_name='registration method', blank=True),
        ),
    ]
