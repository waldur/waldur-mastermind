# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0003_issue_additional_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='issue',
            name='type',
            field=models.CharField(max_length=255),
        ),
    ]
