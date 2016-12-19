# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import nodeconductor.core.validators
import nodeconductor.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0004_paymentdetails_uuid'),
    ]

    operations = [
        migrations.CreateModel(
            name='CompanyType',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[nodeconductor.core.validators.validate_name])),
                ('uuid', nodeconductor.core.fields.UUIDField())
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='paymentdetails',
            name='type',
            field=models.ForeignKey(related_name='+', blank=True, to='invoices.CompanyType', null=True),
        ),
    ]
