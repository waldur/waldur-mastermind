from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations

STORAGE_MODE_FIXED = 'fixed'
STORAGE_MODE_DYNAMIC = 'dynamic'
RAM_TYPE = 'ram'
CORES_TYPE = 'cores'
STORAGE_TYPE = 'storage'
TERMINATED = 6


def import_quotas(offering, quotas, field):
    source_values = {row['name']: row[field] for row in quotas.values('name', field)}
    storage_mode = offering.plugin_options.get('storage_mode') or STORAGE_MODE_FIXED

    result_values = {
        CORES_TYPE: source_values.get('vcpu', 0),
        RAM_TYPE: source_values.get('ram', 0),
    }

    if storage_mode == STORAGE_MODE_FIXED:
        result_values[STORAGE_TYPE] = source_values.get('storage', 0)
    elif storage_mode == STORAGE_MODE_DYNAMIC:
        volume_type_values = {
            k: v for (k, v) in source_values.items() if k.startswith('gigabytes_')
        }
        result_values.update(volume_type_values)

    return result_values


def import_tenant_limits_from_quotas(apps, schema_editor):
    Resource = apps.get_model('marketplace', 'Resource')
    ContentType = apps.get_model('contenttypes', 'ContentType')

    for resource in Resource.objects.filter(
        limits={}, offering__type='Packages.Template'
    ).exclude(state=TERMINATED):
        ct = ContentType.objects.get_for_id(resource.content_type_id)
        model_class = apps.get_model(ct.app_label, ct.model)

        try:
            tenant = model_class.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            print(
                f'Unable to get resource scope with object ID: {resource.object_id}, content type ID: {resource.content_type_id}'
            )
            continue

        resource.limits = import_quotas(resource.offering, tenant.quotas, 'limit')
        resource.save(update_fields=['limits'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0054_restore_limits'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(import_tenant_limits_from_quotas),
    ]
