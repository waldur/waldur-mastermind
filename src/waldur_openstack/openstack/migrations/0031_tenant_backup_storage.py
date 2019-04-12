# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations
from django.contrib.contenttypes.models import ContentType


def cleanup_tenant_quotas(apps, schema_editor):
    Tenant = apps.get_model('openstack', 'Tenant')
    Quota = apps.get_model('quotas', 'Quota')
    quota_names = ['ram', 'volumes_size', 'subnet_count', 'floating_ip_count', 'storage', 'snapshots', 'instances',
                   'network_count', 'volumes', 'vcpu', 'security_group_rule_count', 'snapshots_size',
                   'security_group_count']
    tenant_content_type = ContentType.objects.get_for_model(Tenant)
    Quota.objects.filter(content_type_id=tenant_content_type.id).exclude(name__in=quota_names).delete()


def cleanup_openstackservice_quotas(apps, schema_editor):
    OpenStackService = apps.get_model('openstack', 'OpenStackService')
    Quota = apps.get_model('quotas', 'Quota')
    quota_names = ['tenant_count', 'ram', 'vcpu', 'floating_ip_count', 'storage', 'snapshots', 'instances', 'volumes',
                   'security_group_rule_count', 'security_group_count']
    service_content_type = ContentType.objects.get_for_model(OpenStackService)
    Quota.objects.filter(content_type_id=service_content_type.id).exclude(name__in=quota_names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('openstack', '0030_subnet_dns_nameservers'),
    ]

    operations = [
        migrations.RunPython(cleanup_tenant_quotas),
        migrations.RunPython(cleanup_openstackservice_quotas),
    ]
