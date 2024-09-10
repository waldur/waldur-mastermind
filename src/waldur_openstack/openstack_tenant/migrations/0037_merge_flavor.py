import django.db.models.deletion
from django.db import migrations, models


def migrate_flavor(apps, schema_editor):
    BackupRestoration = apps.get_model("openstack_tenant", "BackupRestoration")
    Flavor = apps.get_model("openstack", "Flavor")
    Tenant = apps.get_model("openstack", "Tenant")

    for backup_restoration in BackupRestoration.objects.all():
        tenant_id = backup_restoration.instance.service_settings.object_id
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            print(f"Tenant {tenant_id} is not found")
            continue
        try:
            backup_restoration.new_flavor = Flavor.objects.get(
                backend_id=backup_restoration.flavor.backend_id,
                settings=tenant.service_settings,
            )
            backup_restoration.save(update_fields=["new_flavor"])
        except Flavor.DoesNotExist:
            print(f"There is no matching flavor {backup_restoration.flavor.backend_id}")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0033_flavor_tenant"),
        ("openstack_tenant", "0036_merge_ports_and_floating_ips"),
    ]

    operations = [
        migrations.AddField(
            model_name="backuprestoration",
            name="new_flavor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="openstack.flavor",
                blank=True,
                null=True,
            ),
        ),
        migrations.RunPython(migrate_flavor),
        migrations.RemoveField(
            model_name="backuprestoration",
            name="flavor",
        ),
        migrations.RenameField(
            model_name="backuprestoration",
            old_name="new_flavor",
            new_name="flavor",
        ),
        migrations.AlterField(
            model_name="backuprestoration",
            name="flavor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="openstack.flavor",
            ),
        ),
        migrations.DeleteModel(
            name="Flavor",
        ),
    ]
