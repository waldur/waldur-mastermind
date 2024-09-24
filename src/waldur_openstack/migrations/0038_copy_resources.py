from django.db import migrations


def copy_resources(apps, schema_editor):
    Tenant = apps.get_model("openstack", "Tenant")
    model_names = (
        "InstanceAvailabilityZone",
        "VolumeAvailabilityZone",
        "Instance",
        "Volume",
        "SnapshotSchedule",
        "BackupSchedule",
        "Snapshot",
        "Backup",
    )

    def get_models(model_name):
        old_model = apps.get_model("openstack_tenant", model_name)
        new_model = apps.get_model("openstack", model_name)
        return old_model, new_model

    for model_name in model_names:
        old_model, new_model = get_models(model_name)
        for row in old_model.objects.all():
            payload = {
                key: val
                for (key, val) in row.__dict__.items()
                if not key.startswith("_")
            }
            if hasattr(row, "settings"):
                tenant = Tenant.objects.get(id=row.settings.object_id)
            else:
                tenant = Tenant.objects.get(id=row.service_settings.object_id)
            payload["tenant"] = tenant
            new_model.objects.create(**payload)

    OldInstance, NewInstance = get_models("Instance")
    for old_vm in OldInstance.objects.all():
        new_vm = NewInstance.objects.get(id=old_vm.id)
        for sg in old_vm.security_groups.all():
            new_vm.security_groups.add(sg)

    OldBackup, NewBackup = get_models("Backup")
    NewSnapshot = apps.get_model("openstack", "Snapshot")
    for old_backup in OldBackup.objects.all():
        new_backup = NewBackup.objects.get(id=old_backup.id)
        for old_snapshot in old_backup.snapshots.all():
            new_snapshot = NewSnapshot.objects.get(id=old_snapshot.id)
            new_backup.snapshots.add(new_snapshot)


def update_resource_content_types(apps, schema_editor):
    def get_models(model_name):
        old_model = apps.get_model("openstack_tenant", model_name)
        new_model = apps.get_model("openstack", model_name)
        return old_model, new_model

    ContentType = apps.get_model("contenttypes", "ContentType")
    Resource = apps.get_model("marketplace", "Resource")

    OldVolume, NewVolume = get_models("Volume")
    OldInstance, NewInstance = get_models("Instance")

    old_instance_content_type = ContentType.objects.get_for_model(OldInstance)
    new_instance_content_type = ContentType.objects.get_for_model(NewInstance)

    old_volume_content_type = ContentType.objects.get_for_model(OldVolume)
    new_volume_content_type = ContentType.objects.get_for_model(NewVolume)

    Resource.objects.filter(content_type=old_instance_content_type).update(
        content_type=new_instance_content_type
    )
    Resource.objects.filter(content_type=old_volume_content_type).update(
        content_type=new_volume_content_type
    )


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0037_add_resource_models"),
        ("openstack_tenant", "0039_merge_volume_type"),
    ]

    operations = [
        migrations.RunPython(copy_resources),
        migrations.RunPython(update_resource_content_types),
    ]
