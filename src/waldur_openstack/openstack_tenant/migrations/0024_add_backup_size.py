# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

from waldur_openstack.openstack_tenant.models import Backup


def add_backup_size_to_metadata(apps, schema_editor):
    for backup in Backup.objects.iterator():
        backup.metadata['size'] = backup.instance.size
        backup.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0023_remove_instance_external_ip'),
    ]

    operations = [
        migrations.RunPython(add_backup_size_to_metadata),
    ]
