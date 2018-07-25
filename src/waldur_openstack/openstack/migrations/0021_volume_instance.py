# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


def migrate_volume_instances(apps, schema_editor):
    Volume = apps.get_model('openstack', 'Volume')

    for volume in Volume.objects.all():
        instance = volume.instances.all().first()
        if instance:
            volume.instance = instance
            volume.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0020_tenant_extra_configuration'),
    ]

    operations = [
        migrations.AddField(
            model_name='volume',
            name='instance',
            field=models.ForeignKey(related_name='+', blank=True, to='openstack.Instance', null=True, on_delete=django.db.models.deletion.SET_NULL),
        ),
        migrations.RunPython(migrate_volume_instances),
        migrations.RemoveField(
            model_name='instance',
            name='volumes',
        ),
        migrations.AlterField(
            model_name='volume',
            name='instance',
            field=models.ForeignKey(related_name='volumes', blank=True, to='openstack.Instance', null=True, on_delete=django.db.models.deletion.SET_NULL),
        ),
    ]
