from django.db import migrations, models


def copy_private_flavors(apps, schema_editor):
    OpenStackFlavor = apps.get_model("openstack", "Flavor")
    Tenant = apps.get_model("openstack", "Tenant")
    OpenStackTenantFlavor = apps.get_model("openstack_tenant", "Flavor")

    tenant_flavor_backend_ids = set(
        OpenStackTenantFlavor.objects.values_list("backend_id", flat=True)
    )
    os_flavor_backend_ids = set(
        OpenStackFlavor.objects.values_list("backend_id", flat=True)
    )
    private_flavor_backend_ids = tenant_flavor_backend_ids - os_flavor_backend_ids

    for tenant_flavor in OpenStackTenantFlavor.objects.filter(
        backend_id__in=private_flavor_backend_ids
    ):
        tenant = Tenant.objects.get(id=tenant_flavor.settings.object_id)
        flavor, _ = OpenStackFlavor.objects.get_or_create(
            settings=tenant.service_settings,
            backend_id=tenant_flavor.backend_id,
            defaults=dict(
                cores=tenant_flavor.cores,
                ram=tenant_flavor.ram,
                disk=tenant_flavor.disk,
                name=tenant_flavor.name,
            ),
        )
        flavor.tenants.add(tenant)


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0032_port_subnet"),
    ]

    operations = [
        migrations.AddField(
            model_name="flavor",
            name="tenants",
            field=models.ManyToManyField(to="openstack.tenant", related_name="flavors"),
        ),
        migrations.RunPython(copy_private_flavors),
    ]
