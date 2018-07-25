# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django_fsm


def migrate_backups_states(apps, schema_editor):
    Backup = apps.get_model('openstack', 'Backup')
    migration_map = {
        1: 3,  # READY -> OK,
        2: 6,  # BACKING_UP -> CREATING,
        3: 3,  # RESTORING -> OK,
        4: 8,  # DELETING -> DELETING,
        5: 4,  # ERRED -> ERRED,
        6: 4,  # DELETED -> ERRED.
    }
    for backup in Backup.objects.all():
        backup.state = migration_map[backup.state]
        backup.tenant = backup.instance.tenant
        backup.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0015_instance_runtime_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='backup',
            name='error_message',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='backup',
            name='tenant',
            field=models.ForeignKey(related_name='backups', to='openstack.Tenant', null=True),
        ),
        migrations.AlterField(
            model_name='backup',
            name='state',
            field=django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')]),
        ),
        migrations.RunPython(migrate_backups_states),
    ]
