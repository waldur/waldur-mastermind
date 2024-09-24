import django.db.models.deletion
from django.db import migrations, models


def migrate_volume_type(apps, schema_editor):
    Volume = apps.get_model("openstack_tenant", "Volume")
    VolumeType = apps.get_model("openstack", "VolumeType")
    Tenant = apps.get_model("openstack", "Tenant")

    for volume in Volume.objects.all().exclude(type__isnull=True):
        tenant_id = volume.service_settings.object_id
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            print(f"Tenant {tenant_id} is not found")
            continue
        try:
            volume.new_type = VolumeType.objects.get(
                backend_id=volume.type.backend_id,
                settings=tenant.service_settings,
            )
            volume.save(update_fields=["new_type"])
        except VolumeType.DoesNotExist:
            print(f"There is no matching VolumeType {volume.type.backend_id}")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0036_merge_volume_type"),
        ("openstack_tenant", "0038_merge_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="volume",
            name="new_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="openstack.volumetype",
            ),
        ),
        migrations.RunPython(migrate_volume_type),
        migrations.RemoveField(
            model_name="volume",
            name="type",
        ),
        migrations.RenameField(
            model_name="volume",
            old_name="new_type",
            new_name="type",
        ),
        migrations.AlterField(
            model_name="volume",
            name="type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="openstack.volumetype",
            ),
        ),
        migrations.DeleteModel(
            name="VolumeType",
        ),
    ]
