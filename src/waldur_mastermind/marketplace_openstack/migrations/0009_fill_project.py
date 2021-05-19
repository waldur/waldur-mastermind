from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def fill_project(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    Tenant = apps.get_model('openstack', 'Tenant')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    service_settings_ct = ContentType.objects.get_for_model(ServiceSettings)
    tenant_ct = ContentType.objects.get_for_model(Tenant)

    for offering in Offering.objects.filter(
        shared=False, content_type=service_settings_ct
    ):
        try:
            service_settings = ServiceSettings.objects.get(id=offering.object_id)
        except ObjectDoesNotExist:
            continue
        if service_settings.content_type != tenant_ct:
            continue
        try:
            tenant = Tenant.objects.get(id=service_settings.object_id)
        except ObjectDoesNotExist:
            continue
        offering.project = tenant.service_project_link.project
        offering.save(update_fields=['project'])


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0050_offering_project'),
        ('marketplace_openstack', '0008_drop_package_tables'),
    ]

    operations = [migrations.RunPython(fill_project)]
