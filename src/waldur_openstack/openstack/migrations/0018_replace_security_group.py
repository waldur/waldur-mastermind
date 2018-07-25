# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


def populate_security_group_instances(apps, schema_editor):
    InstanceSecurityGroup = apps.get_model('openstack', 'InstanceSecurityGroup')

    for isg in InstanceSecurityGroup.objects.all():
        isg.security_group.instances.add(isg.instance)


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0017_backup_snapshots_and_restorations'),
    ]

    operations = [
        migrations.AddField(
            model_name='instance',
            name='security_groups',
            field=models.ManyToManyField(related_name='instances', to='openstack.SecurityGroup'),
        ),
        migrations.RunPython(populate_security_group_instances),
        migrations.RemoveField(
            model_name='instancesecuritygroup',
            name='instance',
        ),
        migrations.RemoveField(
            model_name='instancesecuritygroup',
            name='security_group',
        ),
        migrations.DeleteModel(
            name='InstanceSecurityGroup',
        ),
    ]
