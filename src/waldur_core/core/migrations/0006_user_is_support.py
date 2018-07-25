# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_add_user_language_and_competence'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='is_support',
            field=models.BooleanField(default=False, help_text='Designates whether the user is a global support user.', verbose_name='support status'),
        ),
    ]
