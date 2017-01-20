# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from decimal import Decimal
from django.db import migrations, models
import django.utils.timezone
import django.db.models.deletion
import nodeconductor.core.fields
import model_utils.fields
import nodeconductor.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0005_issue_first_response_sla'),
    ]

    operations = [
        migrations.CreateModel(
            name='Offering',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[nodeconductor.core.validators.validate_name])),
                ('uuid', nodeconductor.core.fields.UUIDField()),
                ('issue', models.ForeignKey(on_delete=django.db.models.deletion.SET_NULL, to='support.Issue', null=True)),
                ('price', models.DecimalField(default=0, help_text='The price per unit of amount', verbose_name='Price per day', max_digits=13, decimal_places=7, validators=[django.core.validators.MinValueValidator(Decimal('0'))])),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
