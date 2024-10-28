from django.db import migrations

ARCHIVED = 4


def copy_resources(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    ServiceSettings = apps.get_model("structure", "ServiceSettings")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Tenant = apps.get_model("openstack", "Tenant")
    Instance = apps.get_model("openstack", "Instance")
    Volume = apps.get_model("openstack", "Volume")
    Backup = apps.get_model("openstack", "Backup")
    Snapshot = apps.get_model("openstack", "Backup")
    BackupSchedule = apps.get_model("openstack", "BackupSchedule")
    SnapshotSchedule = apps.get_model("openstack", "BackupSchedule")
    tenant_content_type = ContentType.objects.get_for_model(Tenant)

    for offering in Offering.objects.exclude(state=ARCHIVED).filter(
        type__in=["OpenStackTenant.Instance", "OpenStackTenant.Volume"]
    ):
        try:
            service_settings = ServiceSettings.objects.get(id=offering.object_id)
        except ServiceSettings.DoesNotExist:
            print("Matching ServiceSettings is not found.")
            print(f"Offering ID: {offering.id}")
            print(f"ServiceSettings ID: {offering.object_id}")
            continue
        try:
            Tenant.objects.get(id=service_settings.object_id)
        except Tenant.DoesNotExist:
            print("Matching tenant is not found.")
            print(f"Service settings ID: {service_settings.id}")
            print(f"Tenant ID: {service_settings.object_id}")
            continue
        offering.object_id = service_settings.object_id
        offering.content_type = tenant_content_type
        offering.save(update_fields=["object_id", "content_type"])

    for model in (Instance, Volume, Backup, Snapshot, BackupSchedule, SnapshotSchedule):
        for row in model.objects.all():
            row.service_settings = row.tenant.service_settings
            row.save(update_fields=["service_settings"])

    ServiceSettings.objects.filter(type="OpenStackTenant").delete()

    Offering.objects.filter(type="OpenStackTenant.Instance").update(
        type="OpenStack.Instance"
    )
    Offering.objects.filter(type="OpenStackTenant.SharedInstance").update(
        type="OpenStack.SharedInstance"
    )
    Offering.objects.filter(type="OpenStackTenant.Volume").update(
        type="OpenStack.Volume"
    )
    Offering.objects.filter(type="OpenStack.Admin").update(type="OpenStack.Tenant")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack_tenant", "0040_drop_all"),
    ]

    operations = [
        migrations.RunPython(copy_resources),
    ]
