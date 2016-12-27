# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0004_issue_users'),
    ]

    operations = [
        migrations.AddField(
            model_name='issue',
            name='first_response_sla',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
