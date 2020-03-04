from django.conf import settings as django_settings
from django.db import migrations


def get_allocation_name(allocation):
    return get_account_name(
        django_settings.WALDUR_SLURM['ALLOCATION_PREFIX'], allocation
    )


def get_account_name(prefix, object_or_uuid):
    key = isinstance(object_or_uuid, str) and object_or_uuid or object_or_uuid.uuid.hex
    return '%s%s' % (prefix, key)


def fill_backend_id(apps, schema_editor):
    Allocation = apps.get_model('waldur_slurm', 'Allocation')
    for allocation in Allocation.objects.all():
        allocation.backend_id = get_allocation_name(allocation)
        allocation.save()


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0006_allocationusage_deposit_usage'),
    ]

    operations = [
        migrations.RunPython(fill_backend_id),
    ]
