import django.db.models.deletion
from django.db import migrations, models


def migrate_subnet(apps, schema_editor):
    InternalIP = apps.get_model("openstack_tenant", "InternalIP")
    NewSubNet = apps.get_model("openstack", "SubNet")

    for internal_ip in InternalIP.objects.all():
        try:
            internal_ip.new_subnet = NewSubNet.objects.get(
                backend_id=internal_ip.subnet.backend_id
            )
            internal_ip.save(update_fields=["new_subnet"])
        except NewSubNet.DoesNotExist:
            print(f"There is no matching SubNet {internal_ip.subnet.backend_id}")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0029_subnet_tenant"),
        ("openstack_tenant", "0034_merge_network"),
    ]

    operations = [
        migrations.AddField(
            model_name="internalip",
            name="new_subnet",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="openstack.subnet",
                blank=True,
                null=True,
            ),
        ),
        migrations.RunPython(migrate_subnet),
        migrations.RemoveField(
            model_name="internalip",
            name="subnet",
        ),
        migrations.RenameField(
            model_name="internalip",
            old_name="new_subnet",
            new_name="subnet",
        ),
        migrations.AlterField(
            model_name="internalip",
            name="subnet",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="internal_ips",
                to="openstack.subnet",
            ),
        ),
        migrations.AlterField(
            model_name="instance",
            name="subnets",
            field=models.ManyToManyField(
                through="openstack_tenant.InternalIP", to="openstack.subnet"
            ),
        ),
        migrations.DeleteModel(
            name="SubNet",
        ),
    ]
