# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def remove_duplicate_floating_ips(apps, schema_editor):
    already_seen = set()
    FloatingIP = apps.get_model('openstack_tenant', 'FloatingIP')

    for fip in FloatingIP.objects.iterator():
        key = (fip.settings_id, fip.backend_id)
        if key in already_seen:
            fip.delete()
        else:
            already_seen.add(key)


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0026_remove_start_time'),
    ]

    operations = [
        migrations.RunPython(remove_duplicate_floating_ips),
    ]
