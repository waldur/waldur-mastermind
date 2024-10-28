import django.db.models.deletion
from django.db import migrations, models


def migrate_network(apps, schema_editor):
    SubNet = apps.get_model("openstack_tenant", "SubNet")
    NewNetwork = apps.get_model("openstack", "Network")

    for subnet in SubNet.objects.all():
        try:
            subnet.new_network = NewNetwork.objects.get(
                backend_id=subnet.network.backend_id
            )
            subnet.save(update_fields=["new_network"])
        except NewNetwork.DoesNotExist:
            print(f"There is no matching Network {subnet.network.backend_id}")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0001_squashed_0028"),
        ("openstack_tenant", "0033_merge_servergroup"),
    ]

    operations = [
        migrations.AddField(
            model_name="subnet",
            name="new_network",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="openstack.network",
                blank=True,
                null=True,
            ),
        ),
        migrations.RunPython(migrate_network),
        migrations.RemoveField(
            model_name="subnet",
            name="network",
        ),
        migrations.RenameField(
            model_name="subnet",
            old_name="new_network",
            new_name="network",
        ),
        migrations.AlterField(
            model_name="subnet",
            name="network",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="openstack.network",
            ),
        ),
        migrations.DeleteModel(
            name="Network",
        ),
    ]
