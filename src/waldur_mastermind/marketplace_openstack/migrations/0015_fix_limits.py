import copy

from django.db import migrations


def fix_limits(apps, schema_editor):
    Resource = apps.get_model("marketplace", "Resource")

    resources = Resource.objects.filter(
        offering__type="OpenStack.Admin", state=2
    ).exclude(limits={})

    for resource in resources:
        updated = False
        old_limits = copy.copy(resource.limits)
        if "storage" in resource.current_usages:
            if resource.limits.get("storage", 0) < resource.current_usages["storage"]:
                resource.limits["storage"] = resource.current_usages["storage"]
                updated = True
        else:
            for k, v in resource.current_usages.items():
                if k.startswith("gigabytes_"):
                    if resource.limits.get(k, 0) < v:
                        resource.limits[k] = v
                        updated = True
        if updated:
            print(
                f"Update limits for {resource} from {old_limits} to {resource.limits}"
            )
            resource.save(update_fields=["limits"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_openstack", "0014_update_router_metadata"),
    ]

    operations = [
        migrations.RunPython(fix_limits),
    ]
