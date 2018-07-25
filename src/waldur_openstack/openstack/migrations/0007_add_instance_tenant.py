# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


def pull_instance_tenant(apps, schema_editor):
    Instance = apps.get_model('openstack', 'Instance')
    for instance in Instance.objects.all():
        instance.tenant = instance.service_project_link.tenants.first()
        instance.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0006_backups_restorations'),
    ]

    operations = [
        migrations.AddField(
            model_name='instance',
            name='tenant',
            field=models.ForeignKey(related_name='instances', to='openstack.Tenant', null=True),
        ),
        migrations.RunPython(pull_instance_tenant),
    ]
