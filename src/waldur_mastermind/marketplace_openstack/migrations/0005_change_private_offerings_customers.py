from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations

INSTANCE_TYPE = 'OpenStackTenant.Instance'

VOLUME_TYPE = 'OpenStackTenant.Volume'


def change_private_offerings_customers(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    Resource = apps.get_model('marketplace', 'Resource')
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    Tenant = apps.get_model('openstack', 'Tenant')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    content_type = ContentType.objects.get_for_model(Tenant)

    for offering in Offering.objects.filter(
        type__in=(INSTANCE_TYPE, VOLUME_TYPE)
    ).all():
        service_settings_id = offering.object_id
        if not service_settings_id:
            continue

        try:
            service_settings = ServiceSettings.objects.get(id=service_settings_id)
        except ObjectDoesNotExist:
            continue

        tenant_id = service_settings.object_id
        if not tenant_id:
            continue

        try:
            resource = Resource.objects.get(
                object_id=tenant_id, content_type=content_type
            )
        except ObjectDoesNotExist:
            continue

        actual_customer = resource.project.customer

        if offering.customer != actual_customer:
            offering.customer = actual_customer
            offering.save(update_fields=['customer'])


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace_openstack', '0004_set_parent_offering'),
    ]

    operations = [migrations.RunPython(change_private_offerings_customers)]
