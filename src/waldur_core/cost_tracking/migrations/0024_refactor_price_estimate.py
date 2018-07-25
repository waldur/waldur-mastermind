# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.core.fields

import waldur_core.cost_tracking.models


class Migration(migrations.Migration):

    dependencies = [
        ('cost_tracking', '0023_consumptiondetails'),
    ]

    operations = [
        migrations.AddField(
            model_name='priceestimate',
            name='parents',
            field=models.ManyToManyField(help_text='Price estimate parents', related_name='children', to='cost_tracking.PriceEstimate'),
        ),
        migrations.AlterField(
            model_name='defaultpricelistitem',
            name='item_type',
            field=models.CharField(help_text='Type of price list item. Examples: storage, flavor.', max_length=255),
        ),
        migrations.AlterField(
            model_name='defaultpricelistitem',
            name='key',
            field=models.CharField(help_text='Key that corresponds particular consumable. Example: name of flavor.', max_length=255),
        ),
        migrations.AlterField(
            model_name='defaultpricelistitem',
            name='metadata',
            field=waldur_core.core.fields.JSONField(blank=True, default={}, help_text='Details of the item, that corresponds price list item. Example: details of flavor.'),
        ),
        migrations.AlterField(
            model_name='priceestimate',
            name='consumed',
            field=models.FloatField(default=0, help_text='Price for resource until now.'),
        ),
        migrations.AlterField(
            model_name='priceestimate',
            name='details',
            field=waldur_core.core.fields.JSONField(default={}, help_text='Saved scope details. Field is populated on scope deletion.'),
        ),
        migrations.AlterField(
            model_name='priceestimate',
            name='limit',
            field=models.FloatField(default=-1, help_text='How many funds object can consume in current month."-1" means no limit.'),
        ),
        migrations.AlterField(
            model_name='priceestimate',
            name='total',
            field=models.FloatField(default=0, help_text='Predicted price for scope for current month.'),
        ),
        migrations.AlterUniqueTogether(
            name='defaultpricelistitem',
            unique_together=set([('key', 'item_type', 'resource_content_type')]),
        ),
        migrations.AlterUniqueTogether(
            name='priceestimate',
            unique_together=set([('content_type', 'object_id', 'month', 'year')]),
        ),
        migrations.RemoveField(
            model_name='priceestimate',
            name='is_manually_input',
        ),
        migrations.RemoveField(
            model_name='priceestimate',
            name='is_visible',
        ),
        migrations.RemoveField(
            model_name='priceestimate',
            name='leafs',
        ),
        migrations.RemoveField(
            model_name='priceestimate',
            name='scope_customer',
        ),
        migrations.AlterField(
            model_name='consumptiondetails',
            name='configuration',
            field=waldur_core.cost_tracking.models.ConsumableItemsField(default={}, help_text='Current resource configuration.'),
        ),
        migrations.AlterField(
            model_name='consumptiondetails',
            name='consumed_before_update',
            field=waldur_core.cost_tracking.models.ConsumableItemsField(default={}, help_text='How many consumables were used by resource before last update.'),
        ),
    ]
