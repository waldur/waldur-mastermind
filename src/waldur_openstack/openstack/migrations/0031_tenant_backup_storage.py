# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

from .. import models


def cleanup_tenant_quotas(apps, schema_editor):
    quota_names = models.Tenant.get_quotas_names()
    for obj in models.Tenant.objects.all():
        obj.quotas.exclude(name__in=quota_names).delete()


def cleanup_openstackservice_quotas(apps, schema_editor):
    quota_names = models.OpenStackService.get_quotas_names()
    for obj in models.OpenStackService.objects.all():
        obj.quotas.exclude(name__in=quota_names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0030_subnet_dns_nameservers'),
    ]

    operations = [
        migrations.RunPython(cleanup_tenant_quotas),
        migrations.RunPython(cleanup_openstackservice_quotas),
    ]
