# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


def pull_floating_ip_tenant(apps, schema_editor):
    FloatingIP = apps.get_model('openstack', 'FloatingIP')
    for floating_ip in FloatingIP.objects.all():
        floating_ip.tenant = floating_ip.service_project_link.tenants.first()
        floating_ip.save()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0010_require_security_group_tenant'),
    ]

    operations = [
        migrations.AddField(
            model_name='floatingip',
            name='tenant',
            field=models.ForeignKey(related_name='floating_ips', to='openstack.Tenant', null=True),
        ),
        migrations.RunPython(pull_floating_ip_tenant)
    ]
