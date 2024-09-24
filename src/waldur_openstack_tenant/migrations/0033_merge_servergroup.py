import django.db.models.deletion
from django.db import migrations, models


def migrate_server_groups(apps, schema_editor):
    Instance = apps.get_model("openstack_tenant", "Instance")
    NewServerGroup = apps.get_model("openstack", "ServerGroup")

    for vm in Instance.objects.all():
        if not vm.server_group:
            continue
        try:
            vm.new_server_group = NewServerGroup.objects.get(
                backend_id=vm.server_group.backend_id
            )
            vm.save(update_fields=["new_server_group"])
        except NewServerGroup.DoesNotExist:
            print(f"There is no matching ServerGroup {vm.server_group.backend_id}")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0001_squashed_0028"),
        ("openstack_tenant", "0032_merge_securitygroup"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="new_server_group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="openstack.servergroup",
            ),
        ),
        migrations.RunPython(migrate_server_groups),
        migrations.RemoveField(
            model_name="instance",
            name="server_group",
        ),
        migrations.RenameField(
            model_name="instance",
            old_name="new_server_group",
            new_name="server_group",
        ),
        migrations.AlterField(
            model_name="instance",
            name="server_group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="openstack.servergroup",
            ),
        ),
        migrations.DeleteModel(
            name="ServerGroup",
        ),
    ]
