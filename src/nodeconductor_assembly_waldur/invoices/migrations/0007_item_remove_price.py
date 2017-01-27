# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from decimal import Decimal
import django.core.validators

from .. import utils


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0006_add_daily_price'),
    ]

    def migrate_daily_price(apps, schema_editor):
        OpenStackItem = apps.get_model('invoices', 'OpenStackItem')
        for item in OpenStackItem.objects.iterator():
            full_days = utils.get_full_days(item.start, item.end)
            daily_price = item.price / full_days if full_days else 0
            item.daily_price = daily_price
            item.save(update_fields=['daily_price'])

    operations = [
        migrations.RunPython(migrate_daily_price),
        migrations.RemoveField(
            model_name='openstackitem',
            name='price',
        ),
    ]
