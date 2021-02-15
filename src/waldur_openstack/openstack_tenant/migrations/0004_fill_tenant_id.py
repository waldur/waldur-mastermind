from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def fill_tenant_id(apps, schema_editor):
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    Tenant = apps.get_model('openstack', 'Tenant')
    for service_settings in ServiceSettings.objects.filter(type='OpenStackTenant'):
        if not service_settings.object_id:
            continue
        try:
            tenant = Tenant.objects.get(id=service_settings.object_id)
        except ObjectDoesNotExist:
            pass
        else:
            if tenant.backend_id and not service_settings.options.get('tenant_id'):
                service_settings.options['tenant_id'] = tenant.backend_id
                service_settings.save(update_fields=['options'])


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0003_extend_description_limits'),
        ('openstack', '0001_squashed_0042'),
    ]

    operations = [
        migrations.RunPython(fill_tenant_id),
    ]
