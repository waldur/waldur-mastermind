from django.db import migrations

INSTANCE_OFFERING_TYPE = "OpenStack.Instance"


def backfill_instance_metadata(apps, schema_editor):
    Resource = apps.get_model("marketplace", "Resource")
    Instance = apps.get_model("openstack", "Instance")

    instances = {
        instance.id: instance
        for instance in Instance.objects.all().only("id", "flavor_name", "image_name")
    }

    for resource in Resource.objects.filter(offering__type=INSTANCE_OFFERING_TYPE):
        instance = instances.get(resource.object_id)
        if instance is None:
            continue
        if (
            resource.backend_metadata.get("flavor_name") == instance.flavor_name
            and resource.backend_metadata.get("image_name") == instance.image_name
        ):
            continue
        resource.backend_metadata["flavor_name"] = instance.flavor_name
        resource.backend_metadata["image_name"] = instance.image_name
        resource.save(update_fields=["backend_metadata"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_openstack", "0018_populate_volume_metadata"),
        ("marketplace", "0237_resourcelimitchangerequest"),
        ("openstack", "0073_instance_config_drive"),
    ]

    operations = [
        migrations.RunPython(backfill_instance_metadata, migrations.RunPython.noop),
    ]
