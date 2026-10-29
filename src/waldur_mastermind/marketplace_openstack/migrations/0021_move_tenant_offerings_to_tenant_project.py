from django.db import migrations

# The tenant's own offerings, as handlers.move_tenant_offerings_with_it moves them.
TENANT_OFFERING_TYPES = ("OpenStack.Instance", "OpenStack.Volume")


def move_tenant_offerings_to_tenant_project(apps, schema_editor):
    """Tenants moved before their offerings went with them left those behind."""
    ContentType = apps.get_model("contenttypes", "ContentType")
    Offering = apps.get_model("marketplace", "Offering")
    Tenant = apps.get_model("openstack", "Tenant")
    tenant_type = ContentType.objects.filter(
        app_label="openstack", model="tenant"
    ).first()
    if tenant_type is None:
        return
    tenants = {
        tenant.pk: tenant
        for tenant in Tenant.objects.filter(
            pk__in=Offering.objects.filter(
                content_type=tenant_type, type__in=TENANT_OFFERING_TYPES
            ).values("object_id")
        ).select_related("project")
    }
    for offering in Offering.objects.filter(
        content_type=tenant_type, type__in=TENANT_OFFERING_TYPES
    ):
        tenant = tenants.get(offering.object_id)
        if tenant is None:
            continue
        if (offering.project_id, offering.customer_id) != (
            tenant.project_id,
            tenant.project.customer_id,
        ):
            Offering.objects.filter(pk=offering.pk).update(
                project_id=tenant.project_id, customer_id=tenant.project.customer_id
            )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_openstack", "0020_instance_metrics"),
        ("openstack", "0089_move_tenant_objects_to_tenant_project"),
        ("marketplace", "0319_offering_qos_max_tres_mins_per_job"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(
            move_tenant_offerings_to_tenant_project, migrations.RunPython.noop
        ),
    ]
