# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


def initialize_backupschedule_project_links(apps, schema_editor):
    BackupSchedule = apps.get_model('openstack_tenant', 'BackupSchedule')
    for backup_schedule in BackupSchedule.objects.iterator():
        backup_schedule.service_project_link = backup_schedule.instance.service_project_link
        backup_schedule.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0012_backupschedule_resource'),
    ]

    operations = [
        migrations.RunPython(initialize_backupschedule_project_links),
    ]
