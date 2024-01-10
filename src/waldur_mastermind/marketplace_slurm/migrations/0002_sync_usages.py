from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def sync_usages(apps, schema_editor):
    Allocation = apps.get_model("waldur_slurm", "Allocation")
    Resource = apps.get_model("marketplace", "Resource")

    for resource in Resource.objects.filter(
        offering__type="SlurmInvoices.SlurmPackage"
    ):
        if not resource.object_id:
            continue
        try:
            allocation = Allocation.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            continue
        resource.current_usages = {
            "cpu": allocation.cpu_usage,
            "gpu": allocation.gpu_usage,
            "ram": allocation.ram_usage,
        }
        resource.save(update_fields=["current_usages"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0001_squashed_0076"),
        (
            "waldur_slurm",
            "0001_squashed_0025_change_validation_for_association_username",
        ),
        ("marketplace_slurm", "0001_sync_limits"),
    ]

    operations = [
        migrations.RunPython(sync_usages),
    ]
