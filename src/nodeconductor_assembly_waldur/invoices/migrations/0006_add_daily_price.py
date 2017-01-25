# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from decimal import Decimal
import django.core.validators

from .. import utils


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0005_add_company_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='openstackitem',
            name='daily_price',
            field=models.DecimalField(default=0, help_text='Price per day.', max_digits=22, decimal_places=7, validators=[django.core.validators.MinValueValidator(Decimal('0'))]),
        ),
    ]
