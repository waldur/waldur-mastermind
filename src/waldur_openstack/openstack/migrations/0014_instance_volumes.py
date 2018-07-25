# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from uuid import uuid4

from django.db import migrations, models


def populate_instance_volumes(apps, schema_editor):
    Instance = apps.get_model('openstack', 'Instance')
    Volume = apps.get_model('openstack', 'Volume')
    for instance in Instance.objects.all():
        if instance.system_volume_id:
            system_volume = Volume.objects.create(
                uuid=uuid4().hex,
                tenant=instance.tenant,
                service_project_link=instance.service_project_link,
                bootable=True,
                size=instance.system_volume_size,
                backend_id=instance.system_volume_id,
                name='{0}-system'.format(instance.name[:143]),
                state=3,
            )
            instance.volumes.add(system_volume)
        if instance.data_volume_id:
            data_volume = Volume.objects.create(
                uuid=uuid4().hex,
                tenant=instance.tenant,
                service_project_link=instance.service_project_link,
                bootable=False,
                size=instance.data_volume_size,
                backend_id=instance.data_volume_id,
                name='{0}-data'.format(instance.name[:145]),
                state=3,
            )
            instance.volumes.add(data_volume)


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0013_add_dr_backups_to_schedule'),
    ]

    operations = [
        migrations.AddField(
            model_name='instance',
            name='volumes',
            field=models.ManyToManyField(related_name='instances', to='openstack.Volume'),
        ),
        migrations.RunPython(populate_instance_volumes),
        migrations.RemoveField(
            model_name='drbackup',
            name='instance_volumes',
        ),
        migrations.RemoveField(
            model_name='instance',
            name='data_volume_id',
        ),
        migrations.RemoveField(
            model_name='instance',
            name='data_volume_size',
        ),
        migrations.RemoveField(
            model_name='instance',
            name='system_volume_id',
        ),
        migrations.RemoveField(
            model_name='instance',
            name='system_volume_size',
        ),
    ]
