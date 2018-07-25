# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
import model_utils.fields
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('cost_tracking', '0022_priceestimate_leafs'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConsumptionDetails',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('configuration', waldur_core.core.fields.JSONField(default={}, help_text='Current resource configuration.')),
                ('last_update_time', models.DateTimeField(help_text='Last configuration change time.')),
                ('consumed_before_update', waldur_core.core.fields.JSONField(default={}, help_text='How many consumables were used by resource before last update.')),
                ('price_estimate', models.OneToOneField(related_name='consumption_details', to='cost_tracking.PriceEstimate')),
            ],
            options={
                'abstract': False,
                'verbose_name': 'Consumption details',
                'verbose_name_plural': 'Consumption details',
            },
        ),
    ]
