# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from decimal import Decimal
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0005_add_company_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='price',
            field=models.DecimalField(default=0, help_text='Price per day.', max_digits=13, decimal_places=7, validators=[django.core.validators.MinValueValidator(Decimal('0'))]),
        ),
        migrations.AlterField(
            model_name='openstackitem',
            name='price',
            field=models.DecimalField(help_text='Price per day.', max_digits=13, decimal_places=7, validators=[django.core.validators.MinValueValidator(Decimal('0'))]),
        ),
    ]
