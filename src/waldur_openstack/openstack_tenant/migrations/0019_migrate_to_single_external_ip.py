# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


def copy_external_ips(apps, schema_editor):
    Instance = apps.get_model('openstack_tenant', 'Instance')
    FloatingIP = apps.get_model('openstack_tenant', 'FloatingIP')

    for instance in Instance.objects.iterator():
        floating_ip = FloatingIP.objects.filter(
            settings=instance.service_project_link.service.settings,
            address=instance.external_ips,
        ).first()

        if floating_ip:
            instance.external_ip = floating_ip
            instance.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0018_remove_openstacktenantservice_name'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='instance',
            name='internal_ips',
        ),
        migrations.AddField(
            model_name='instance',
            name='external_ip',
            field=models.OneToOneField(related_name='instance', to='openstack_tenant.FloatingIP', blank=True, null=True, on_delete=models.SET_NULL),
        ),
        migrations.RunPython(copy_external_ips),
    ]
