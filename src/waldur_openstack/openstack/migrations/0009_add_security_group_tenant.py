# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


def pull_security_group_tenant(apps, schema_editor):
    SecurityGroup = apps.get_model('openstack', 'SecurityGroup')
    for group in SecurityGroup.objects.all():
        group.tenant = group.service_project_link.tenants.first()
        group.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0008_require_instance_tenant'),
    ]

    operations = [
        migrations.AddField(
            model_name='securitygroup',
            name='tenant',
            field=models.ForeignKey(related_name='security_groups', to='openstack.Tenant', null=True),
        ),
        migrations.RunPython(pull_security_group_tenant)
    ]
