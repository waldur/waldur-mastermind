# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations
from django.contrib.contenttypes.models import ContentType


def initialize_subnet_count_quota(apps, schema_editor):
    Tenant = apps.get_model('openstack', 'Tenant')
    Quota = apps.get_model('quotas', 'Quota')
    tenant_content_type = ContentType.objects.get_for_model(Tenant)
    quota_name = 'subnet_count'
    tenant_ids = Quota.objects.filter(name=quota_name, content_type_id=tenant_content_type.id). \
        values_list('object_id', flat=True)

    for tenant in Tenant.objects.exclude(id__in=tenant_ids).iterator():
        Quota.objects.create(
            name=quota_name,
            limit=10,
            usage=tenant.networks.count(),
            content_type_id=tenant_content_type.id,
            object_id=tenant.id)


def initialize_network_count_quota(apps, schema_editor):
    Tenant = apps.get_model('openstack', 'Tenant')
    SubNet = apps.get_model('openstack', 'SubNet')
    Quota = apps.get_model('quotas', 'Quota')
    tenant_content_type = ContentType.objects.get_for_model(Tenant)
    quota_name = 'network_count'
    tenant_ids = Quota.objects.filter(name=quota_name, content_type_id=tenant_content_type.id).\
        values_list('object_id', flat=True)

    for tenant in Tenant.objects.exclude(id__in=tenant_ids).iterator():
        subnet_count = SubNet.objects.filter(network__pk__in=tenant.networks.values('pk')).count()
        Quota.objects.create(
            name=quota_name,
            limit=10,
            usage=subnet_count,
            content_type_id=tenant_content_type.id,
            object_id=tenant.id)


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('quotas', '0004_quota_threshold'),
        ('openstack', '0028_security_group_as_a_resource'),
    ]

    operations = [
        migrations.RunPython(initialize_network_count_quota),
        migrations.RunPython(initialize_subnet_count_quota),
    ]
