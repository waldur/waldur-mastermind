from django.db import migrations, models


def copy_private_volume_types(apps, schema_editor):
    OpenStackVolumeType = apps.get_model("openstack", "VolumeType")
    Tenant = apps.get_model("openstack", "Tenant")
    OpenStackTenantVolumeType = apps.get_model("openstack_tenant", "VolumeType")

    tenant_volume_type_backend_ids = set(
        OpenStackTenantVolumeType.objects.values_list("backend_id", flat=True)
    )
    os_volume_type_backend_ids = set(
        OpenStackVolumeType.objects.values_list("backend_id", flat=True)
    )
    private_volume_type_backend_ids = (
        tenant_volume_type_backend_ids - os_volume_type_backend_ids
    )

    for tenant_volume_type in OpenStackVolumeType.objects.filter(
        backend_id__in=private_volume_type_backend_ids
    ):
        tenant = Tenant.objects.get(id=tenant_volume_type.settings.object_id)
        volume_type, _ = OpenStackVolumeType.objects.get_or_create(
            settings=tenant.service_settings,
            backend_id=tenant_volume_type.backend_id,
            defaults=dict(
                name=tenant_volume_type.name,
                description=tenant_volume_type.description,
            ),
        )
        volume_type.tenants.add(tenant)


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
