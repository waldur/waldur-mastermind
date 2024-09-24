from django.db import migrations, models


def copy_private_images(apps, schema_editor):
    OpenStackImage = apps.get_model("openstack", "Image")
    Tenant = apps.get_model("openstack", "Tenant")
    OpenStackTenantImage = apps.get_model("openstack_tenant", "Image")

    tenant_image_backend_ids = set(
        OpenStackTenantImage.objects.values_list("backend_id", flat=True)
    )
    os_image_backend_ids = set(
        OpenStackImage.objects.values_list("backend_id", flat=True)
    )
    private_image_backend_ids = tenant_image_backend_ids - os_image_backend_ids

    for tenant_image in OpenStackImage.objects.filter(
        backend_id__in=private_image_backend_ids
    ):
        tenant = Tenant.objects.get(id=tenant_image.settings.object_id)
        image, _ = OpenStackImage.objects.get_or_create(
            settings=tenant.service_settings,
            backend_id=tenant_image.backend_id,
            defaults=dict(
                min_disk=tenant_image.min_disk,
                min_ram=tenant_image.min_ram,
                name=tenant_image.name,
            ),
        )
        image.tenants.add(tenant)


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0033_flavor_tenant"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="tenants",
            field=models.ManyToManyField(to="openstack.tenant", related_name="images"),
        ),
        migrations.RunPython(copy_private_images),
    ]
