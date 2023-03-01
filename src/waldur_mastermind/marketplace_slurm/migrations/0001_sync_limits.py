from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def sync_limits(apps, schema_editor):
    Allocation = apps.get_model('waldur_slurm', 'Allocation')
    Resource = apps.get_model('marketplace', 'Resource')

    for resource in Resource.objects.filter(
        offering__type='SlurmInvoices.SlurmPackage'
    ):
        if not resource.object_id:
            continue
        try:
            allocation = Allocation.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            pass
        resource.limits = {
            'cpu': allocation.cpu_limit,
            'gpu': allocation.gpu_limit,
            'ram': allocation.ram_limit,
        }
        resource.save(update_fields=['limits'])


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0061_order_item_review'),
        ('waldur_slurm', '0024_change_default_allocation_limits'),
    ]

    operations = [
        migrations.RunPython(sync_limits),
    ]
