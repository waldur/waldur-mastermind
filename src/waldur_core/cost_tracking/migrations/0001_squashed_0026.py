# -*- coding: utf-8 -*-
import django.core.validators
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.validators
import waldur_core.cost_tracking.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('structure', '0001_squashed_0054'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConsumptionDetails',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('configuration', waldur_core.cost_tracking.models.ConsumableItemsField(default={}, help_text='Current resource configuration.')),
                ('last_update_time', models.DateTimeField(help_text='Last configuration change time.')),
                ('consumed_before_update', waldur_core.cost_tracking.models.ConsumableItemsField(default={}, help_text='How many consumables were used by resource before last update.')),
            ],
            options={
                'verbose_name': 'Consumption details',
                'verbose_name_plural': 'Consumption details',
            },
        ),
        migrations.CreateModel(
            name='DefaultPriceListItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('value', models.DecimalField(decimal_places=7, default=0, max_digits=13, verbose_name='Hourly rate')),
                ('units', models.CharField(blank=True, max_length=255)),
                ('item_type', models.CharField(help_text='Type of price list item. Examples: storage, flavor.', max_length=255)),
                ('key', models.CharField(help_text='Key that corresponds particular consumable. Example: name of flavor.', max_length=255)),
                ('metadata', waldur_core.core.fields.JSONField(blank=True, default={}, help_text='Details of the item, that corresponds price list item. Example: details of flavor.')),
                ('resource_content_type', models.ForeignKey(default=None, on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType')),
            ],
        ),
        migrations.CreateModel(
            name='PriceEstimate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('object_id', models.PositiveIntegerField(null=True)),
                ('details', waldur_core.core.fields.JSONField(default={}, help_text='Saved scope details. Field is populated on scope deletion.')),
                ('total', models.FloatField(default=0, help_text='Predicted price for scope for current month.')),
                ('consumed', models.FloatField(default=0, help_text='Price for resource until now.')),
                ('month', models.PositiveSmallIntegerField(validators=[django.core.validators.MaxValueValidator(12), django.core.validators.MinValueValidator(1)])),
                ('year', models.PositiveSmallIntegerField()),
                ('content_type', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType')),
                ('parents', models.ManyToManyField(help_text='Price estimate parents', related_name='children', to='cost_tracking.PriceEstimate')),
            ],
            bases=(waldur_core.logging.loggers.LoggableMixin, models.Model, waldur_core.core.models.DescendantMixin),
        ),
        migrations.CreateModel(
            name='PriceListItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('value', models.DecimalField(decimal_places=7, default=0, max_digits=13, verbose_name='Hourly rate')),
                ('units', models.CharField(blank=True, max_length=255)),
                ('object_id', models.PositiveIntegerField()),
                ('content_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType')),
                ('default_price_list_item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='cost_tracking.DefaultPriceListItem')),
            ],
        ),
        migrations.AddField(
            model_name='consumptiondetails',
            name='price_estimate',
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='consumption_details', to='cost_tracking.PriceEstimate'),
        ),
        migrations.AlterUniqueTogether(
            name='pricelistitem',
            unique_together=set([('content_type', 'object_id', 'default_price_list_item')]),
        ),
        migrations.AlterUniqueTogether(
            name='priceestimate',
            unique_together=set([('content_type', 'object_id', 'month', 'year')]),
        ),
        migrations.AlterUniqueTogether(
            name='defaultpricelistitem',
            unique_together=set([('key', 'item_type', 'resource_content_type')]),
        ),
    ]
