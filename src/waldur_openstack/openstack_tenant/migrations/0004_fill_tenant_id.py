from django.db import migrations


def fill_tenant_id(apps, schema_editor):
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    for service_settings in ServiceSettings.objects.filter(type='OpenStackTenant'):
        tenant = service_settings.scope
        if (
            tenant
            and tenant.backend_id
            and not service_settings.options.get('tenant_id')
        ):
            service_settings.options['tenant_id'] = tenant.backend_id
            service_settings.save(update_fields=['options'])


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0003_extend_description_limits'),
    ]

    operations = [
        migrations.RunPython(fill_tenant_id),
    ]
