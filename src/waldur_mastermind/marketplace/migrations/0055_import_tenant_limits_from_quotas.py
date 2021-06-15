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
    Quota = apps.get_model('quotas', 'Quota')

    for resource in Resource.objects.filter(
        limits={}, offering__type='Packages.Template'
    ).exclude(state=TERMINATED):
        quotas = Quota.objects.filter(
            content_type_id=resource.content_type_id, object_id=resource.object_id
        )
        resource.limits = import_quotas(resource.offering, quotas, 'limit')
        resource.save(update_fields=['limits'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0054_restore_limits'),
    ]

    operations = [
        migrations.RunPython(import_tenant_limits_from_quotas),
    ]
