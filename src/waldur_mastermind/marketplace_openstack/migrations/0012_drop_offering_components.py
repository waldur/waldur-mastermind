from django.db import migrations

TENANT_TYPE = 'Packages.Template'
STORAGE_MODE_FIXED = 'fixed'


def drop_offering_components(apps, schema_editor):
    """
    Drop offering components for volume types if storage mode is fixed.
    """
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')
    OfferingComponent.objects.filter(
        offering__type=TENANT_TYPE,
        offering__plugin_options__storage_mode=STORAGE_MODE_FIXED,
        type__startswith='gigabytes_',
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0050_offering_project'),
        ('marketplace_openstack', '0011_limit_components'),
    ]

    operations = [migrations.RunPython(drop_offering_components)]
