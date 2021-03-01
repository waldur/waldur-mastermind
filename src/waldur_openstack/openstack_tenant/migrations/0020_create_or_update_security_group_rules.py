from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import migrations


def create_or_update_security_group_rules(apps, schema_editor):
    SecurityGroupRuleResource = apps.get_model('openstack', 'SecurityGroupRule')
    SecurityGroupProperty = apps.get_model('openstack_tenant', 'SecurityGroup')
    SecurityGroupRuleProperty = apps.get_model('openstack_tenant', 'SecurityGroupRule')
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    Tenant = apps.get_model('openstack', 'Tenant')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    content_type = ContentType.objects.get_for_model(Tenant)

    for rule_resource in SecurityGroupRuleResource.objects.exclude(backend_id=''):
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
            remote_group = None
            if rule_resource.remote_group and rule_resource.remote_group.backend_id:
                try:
                    remote_group = SecurityGroupProperty.objects.get(
                        settings=service_settings,
                        backend_id=rule_resource.remote_group.backend_id,
                    )
                except ObjectDoesNotExist:
                    pass
            SecurityGroupRuleProperty.objects.update_or_create(
                security_group=security_group,
                backend_id=rule_resource.backend_id,
                defaults=dict(
                    ethertype=rule_resource.ethertype,
                    direction=rule_resource.direction,
                    protocol=rule_resource.protocol,
                    from_port=rule_resource.from_port,
                    to_port=rule_resource.to_port,
                    cidr=rule_resource.cidr,
                    description=rule_resource.description,
                    remote_group=remote_group,
                ),
            )
        except ObjectDoesNotExist:
            continue
        except MultipleObjectsReturned:
            print(rule_resource)


class Migration(migrations.Migration):

    dependencies = [
        (
            'openstack_tenant',
            '0019_enforce_uniqueness_constraint_on_security_group_rule',
        ),
    ]

    operations = [
        migrations.RunPython(create_or_update_security_group_rules),
    ]
