# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

from waldur_mastermind.common.utils import quantize_price


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0008_offeringitem'),
    ]

    def quantize_prices(apps, schema_editor):
        OpenStackItem = apps.get_model('invoices', 'OpenStackItem')
        for item in OpenStackItem.objects.iterator():
            item.daily_price = quantize_price(item.daily_price)
            item.save(update_fields=['daily_price'])

    operations = [
        migrations.RunPython(quantize_prices),
    ]
