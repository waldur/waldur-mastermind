# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django_fsm


def migrate_instance_state(apps, schema_editor):
    Instance = apps.get_model('openstack', 'Instance')
    # old states
    PROVISIONING_SCHEDULED = 1
    PROVISIONING = 2
    ONLINE = 3
    OFFLINE = 4
    STARTING_SCHEDULED = 5
    STARTING = 6
    STOPPING_SCHEDULED = 7
    STOPPING = 8
    ERRED = 9
    DELETION_SCHEDULED = 10
    DELETING = 11
    RESIZING_SCHEDULED = 13
    RESIZING = 14
    RESTARTING_SCHEDULED = 15
    RESTARTING = 16
    # new states
    NEW_CREATION_SCHEDULED = 5
    NEW_CREATING = 6
    NEW_UPDATE_SCHEDULED = 1
    NEW_UPDATING = 2
    NEW_DELETION_SCHEDULED = 7
    NEW_DELETING = 8
    NEW_OK = 3
    NEW_ERRED = 4

    state_map = {
        PROVISIONING_SCHEDULED: NEW_CREATION_SCHEDULED,
        PROVISIONING: NEW_CREATING,
        ONLINE: NEW_OK,
        OFFLINE: NEW_OK,
        STARTING_SCHEDULED: NEW_UPDATE_SCHEDULED,
        STARTING: NEW_UPDATING,
        STOPPING_SCHEDULED: NEW_UPDATE_SCHEDULED,
        STOPPING: NEW_UPDATING,
        ERRED: NEW_ERRED,
        DELETION_SCHEDULED: NEW_DELETION_SCHEDULED,
        DELETING: NEW_DELETING,
        RESIZING_SCHEDULED: NEW_UPDATE_SCHEDULED,
        RESIZING: NEW_UPDATING,
        RESTARTING_SCHEDULED: NEW_UPDATE_SCHEDULED,
        RESTARTING: NEW_UPDATING,
    }

    for instance in Instance.objects.all():
        instance.state = state_map[instance.state]
        instance.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0022_volume_device'),
    ]

    operations = [
        migrations.RunPython(migrate_instance_state),
        migrations.AlterField(
            model_name='instance',
            name='state',
            field=django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')]),
        ),
    ]
