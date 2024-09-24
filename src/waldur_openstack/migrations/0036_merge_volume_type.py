from django.db import migrations, models


def copy_private_volume_types(apps, schema_editor):
    OldVolumeType = apps.get_model("openstack_tenant", "VolumeType")
    NewVolumeType = apps.get_model("openstack", "VolumeType")
    Tenant = apps.get_model("openstack", "Tenant")

    old_backend_ids = set(OldVolumeType.objects.values_list("backend_id", flat=True))
    new_backend_ids = set(NewVolumeType.objects.values_list("backend_id", flat=True))
    private_backend_ids = old_backend_ids - new_backend_ids

    for old_volume_type in OldVolumeType.objects.filter(
        backend_id__in=private_backend_ids
    ):
        tenant = Tenant.objects.get(id=old_volume_type.settings.object_id)
        new_volume_type, _ = NewVolumeType.objects.get_or_create(
            settings=tenant.service_settings,
            backend_id=old_volume_type.backend_id,
            defaults=dict(
                name=old_volume_type.name,
                description=old_volume_type.description,
            ),
        )
        new_volume_type.tenants.add(tenant)


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0035_drop_flavor_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="volumetype",
            name="tenants",
            field=models.ManyToManyField(
                related_name="volume_types", to="openstack.tenant"
            ),
        ),
        migrations.RunPython(copy_private_volume_types),
    ]
