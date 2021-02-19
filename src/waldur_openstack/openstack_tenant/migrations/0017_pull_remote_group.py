from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def pull_remote_group(apps, schema_editor):
    SecurityGroupRuleResource = apps.get_model('openstack', 'SecurityGroupRule')
    SecurityGroupProperty = apps.get_model('openstack_tenant', 'SecurityGroup')
    SecurityGroupRuleProperty = apps.get_model('openstack_tenant', 'SecurityGroupRule')
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    Tenant = apps.get_model('openstack', 'Tenant')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    content_type = ContentType.objects.get_for_model(Tenant)

    for rule_resource in SecurityGroupRuleResource.objects.exclude(remote_group=None):
        try:
            service_settings = ServiceSettings.objects.get(
                type='OpenStackTenant',
                content_type=content_type,
                object_id=rule_resource.security_group.tenant_id,
            )
            security_group = SecurityGroupProperty.objects.get(
                settings=service_settings,
                backend_id=rule_resource.security_group.backend_id,
            )
            remote_group = SecurityGroupProperty.objects.get(
                settings=service_settings,
                backend_id=rule_resource.remote_group.backend_id,
            )
            rule_property = SecurityGroupRuleProperty.objects.get(
                security_group=security_group, backend_id=rule_resource.backend_id
            )
        except ObjectDoesNotExist:
            continue
        else:
            if rule_property.remote_group != remote_group:
                rule_property.remote_group = remote_group
                rule_property.save(update_fields=['remote_group'])


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0016_internalip_device_info'),
    ]

    operations = [
        migrations.RunPython(pull_remote_group),
    ]
