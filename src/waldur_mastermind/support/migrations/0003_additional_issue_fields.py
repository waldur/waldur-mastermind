# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0002_comment_and_support_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='issue',
            name='impact',
            field=models.CharField(max_length=255, blank=True),
        ),
        migrations.AddField(
            model_name='issue',
            name='link',
            field=models.URLField(help_text='Link to issue in support system.', max_length=255, blank=True),
        ),
        migrations.AddField(
            model_name='issue',
            name='priority',
            field=models.CharField(max_length=255, blank=True),
        ),
        migrations.AlterField(
            model_name='issue',
            name='type',
            field=models.CharField(max_length=255),
        ),
    ]
