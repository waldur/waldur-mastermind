from django.db import migrations


def populate_volume_metadata(apps, schema_editor):
    Resource = apps.get_model("marketplace", "Resource")

    # Filter for OpenStack Volume offerings
    for resource in Resource.objects.filter(offering__type="OpenStack.Volume"):
        # Check if backend_metadata is missing size but attributes has it
        size = resource.attributes.get("size")
        if size and not resource.backend_metadata.get("size"):
            resource.backend_metadata["size"] = size
            resource.save(update_fields=["backend_metadata"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_openstack", "0017_rename_internal_ips_set"),
    ]

    operations = [
        migrations.RunPython(populate_volume_metadata),
    ]
