# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0002_invoice_additional_details'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentdetails',
            name='default_tax_percent',
            field=models.DecimalField(default=0, max_digits=4, decimal_places=2, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)]),
        ),
    ]
