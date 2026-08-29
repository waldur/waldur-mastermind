from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def sync_limits(apps, schema_editor):
    Allocation = apps.get_model("waldur_openportal", "Allocation")
    Resource = apps.get_model("marketplace", "Resource")

    for resource in Resource.objects.filter(
        offering__type="Marketplace.OpenPortalRemote"
    ):
        if not resource.object_id:
            continue
        try:
            allocation = Allocation.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            continue
        resource.limits = {
            "node": allocation.node_limit,
        }
        resource.save(update_fields=["limits"])


def sync_usages(apps, schema_editor):
    Allocation = apps.get_model("waldur_openportal", "Allocation")
    Resource = apps.get_model("marketplace", "Resource")

    for resource in Resource.objects.filter(
        offering__type="Marketplace.OpenPortalRemote"
    ):
        if not resource.object_id:
            continue
        try:
            allocation = Allocation.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            continue
        resource.current_usages = {
            "node": allocation.node_usage,
        }
        resource.save(update_fields=["current_usages"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0001_squashed_0076"),
        ("waldur_openportal", "0001_squashed_0028_delete_remoteallocationuserusage"),
    ]

    operations = [
        migrations.RunPython(sync_limits),
        migrations.RunPython(sync_usages),
    ]
