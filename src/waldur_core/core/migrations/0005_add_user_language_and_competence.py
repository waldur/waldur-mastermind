# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_user_agreement_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='competence',
            field=models.CharField(max_length=255, blank=True),
        ),
        migrations.AddField(
            model_name='user',
            name='preferred_language',
            field=models.CharField(max_length=10, blank=True),
        ),
        migrations.AlterField(
            model_name='user',
            name='phone_number',
            field=models.CharField(max_length=255, verbose_name='phone number', blank=True),
        ),
    ]
