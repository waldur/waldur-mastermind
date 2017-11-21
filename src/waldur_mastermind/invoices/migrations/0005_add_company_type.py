# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0004_paymentdetails_uuid'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentdetails',
            name='type',
            field=models.CharField(max_length=150, blank=True),
        ),
    ]
