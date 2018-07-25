# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations
import waldur_core.core.fields
import django.core.validators

import waldur_core.core.validators


class Migration(migrations.Migration):

    #replaces = [('cost_tracking', '0001_initial'), ('cost_tracking', '0002_price_list'), ('cost_tracking', '0003_new_price_list_items'), ('cost_tracking', '0004_remove_connection_to_resource'), ('cost_tracking', '0005_expand_item_type_size'), ('cost_tracking', '0006_add_backend_cache_fields_to_pricelist'), ('cost_tracking', '0007_remove_obsolete_billing_fields'), ('cost_tracking', '0008_delete_resourceusage'), ('cost_tracking', '0009_defaultpricelistitem_name'), ('cost_tracking', '0010_applicationtype'), ('cost_tracking', '0011_applicationtype_slug'), ('cost_tracking', '0012_fix_fields_length')]

    dependencies = [
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PriceEstimate',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('object_id', models.PositiveIntegerField()),
                ('total', models.FloatField(default=0)),
                ('details', waldur_core.core.fields.JSONField(blank=True)),
                ('month', models.PositiveSmallIntegerField(validators=[django.core.validators.MaxValueValidator(12), django.core.validators.MinValueValidator(1)])),
                ('year', models.PositiveSmallIntegerField()),
                ('is_manually_input', models.BooleanField(default=False)),
                ('is_visible', models.BooleanField(default=True)),
                ('content_type', models.ForeignKey(to='contenttypes.ContentType')),
            ],
            options={
                'unique_together': set([('content_type', 'object_id', 'month', 'year', 'is_manually_input')]),
            },
            bases=(models.Model,),
        ),
        migrations.CreateModel(
            name='PriceListItem',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('content_type', models.ForeignKey(to='contenttypes.ContentType')),
                ('object_id', models.PositiveIntegerField()),
                ('key', models.CharField(max_length=255)),
                ('value', models.DecimalField(default=0, verbose_name=b'Hourly rate', max_digits=9, decimal_places=2)),
                ('units', models.CharField(max_length=255, blank=True)),
                ('item_type', models.CharField(default=b'flavor', max_length=255, choices=[(b'flavor', b'flavor'), (b'storage', b'storage'), (b'license-application', b'license-application'), (b'license-os', b'license-os'), (b'support', b'support'), (b'network', b'network'), (b'usage', b'usage'), (b'users', b'users')])),
                ('is_manually_input', models.BooleanField(default=False)),
                ('resource_content_type', models.ForeignKey(related_name='+', default=None, to='contenttypes.ContentType')),
            ],
            options={
                'unique_together': set([('key', 'content_type', 'object_id')]),
            },
            bases=(models.Model,),
        ),
        migrations.CreateModel(
            name='DefaultPriceListItem',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('key', models.CharField(max_length=255)),
                ('value', models.DecimalField(default=0, verbose_name=b'Hourly rate', max_digits=9, decimal_places=2)),
                ('units', models.CharField(max_length=255, blank=True)),
                ('item_type', models.CharField(default=b'flavor', max_length=255, choices=[(b'flavor', b'flavor'), (b'storage', b'storage'), (b'license-application', b'license-application'), (b'license-os', b'license-os'), (b'support', b'support'), (b'network', b'network'), (b'usage', b'usage'), (b'users', b'users')])),
                ('resource_content_type', models.ForeignKey(default=None, to='contenttypes.ContentType')),
            ],
            options={
                'abstract': False,
            },
            bases=(models.Model,),
        ),
        migrations.CreateModel(
            name='ApplicationType',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('slug', models.CharField(unique=True, max_length=150)),
            ],
            options={
                'abstract': False,
            },
            bases=(models.Model,),
        ),
    ]
