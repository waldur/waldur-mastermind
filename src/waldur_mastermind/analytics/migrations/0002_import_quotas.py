# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def import_quotas(apps, schema_editor):
    from waldur_mastermind.analytics.utils import import_daily_usage

    import_daily_usage()


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('analytics', '0001_initial'),
        ('quotas', '0001_squashed_0004'),
        ('reversion', '0001_squashed_0004_auto_20160611_1202'),
    ]

    operations = [
        migrations.RunPython(import_quotas),
    ]
