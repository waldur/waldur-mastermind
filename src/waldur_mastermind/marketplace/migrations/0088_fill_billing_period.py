# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

from waldur_core.core.utils import month_start


def fill_billing_period(apps, schema_editor):
    ComponentUsage = apps.get_model('marketplace', 'ComponentUsage')
    for item in ComponentUsage.objects.all():
        item.billing_period = month_start(item.date)
        item.save(update_fields=['billing_period'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0087_component_usage_billing_period'),
    ]

    operations = [
        migrations.RunPython(fill_billing_period)
    ]
