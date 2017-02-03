# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from decimal import Decimal

import django
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0006_offering'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='offering',
            options={'ordering': ['-created']},
        ),
        migrations.RemoveField(
            model_name='offering',
            name='type_label',
        ),
        migrations.AlterField(
            model_name='offering',
            name='price',
            field=models.DecimalField(decimal_places=7, default=0, max_digits=13, validators=[django.core.validators.MinValueValidator(Decimal('0'))], help_text='The price per unit', verbose_name='Price per day'),
        ),
    ]
