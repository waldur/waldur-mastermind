# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations
from waldur_core.quotas import models as quotas_models
from django.contrib.contenttypes.models import ContentType

from .. import models


def initialize_subnet_count_quota(apps, schema_editor):
    tenant_content_type = ContentType.objects.get_for_model(models.Tenant)
    quota_name = 'subnet_count'

    for tenant in models.Tenant.objects.exclude(quotas__name=quota_name).iterator():
        tenant.quotas.add(quotas_models.Quota(
            name=quota_name,
            limit=10,
            usage=tenant.networks.count(),
            content_type=tenant_content_type))


def initialize_network_count_quota(apps, schema_editor):
    tenant_content_type = ContentType.objects.get_for_model(models.Tenant)
    quota_name = 'network_count'

    for tenant in models.Tenant.objects.exclude(quotas__name=quota_name).iterator():
        subnet_count = models.SubNet.objects.filter(network__pk__in=tenant.networks.values('pk')).count()
        tenant.quotas.add(quotas_models.Quota(
            name=quota_name,
            limit=10,
            usage=subnet_count,
            content_type=tenant_content_type))


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0028_security_group_as_a_resource'),
    ]

    operations = [
        migrations.RunPython(initialize_network_count_quota),
        migrations.RunPython(initialize_subnet_count_quota),
    ]
