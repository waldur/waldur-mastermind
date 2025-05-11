import django.db.models.deletion
from django.db import migrations, models


def copy_object_id_to_new_instance(apps, schema_editor):
    Node = apps.get_model("waldur_rancher", "Node")
    Node.objects.update(new_instance_id=models.F("object_id"))


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0050_port_status"),
        ("waldur_rancher", "0046_cluster_vm_project"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="node",
            unique_together={("cluster", "name")},
        ),
        migrations.AddField(
            model_name="node",
            name="new_instance",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="openstack.instance",
            ),
        ),
        migrations.RunPython(
            code=copy_object_id_to_new_instance,
        ),
        migrations.RemoveField(
            model_name="node",
            name="content_type",
        ),
        migrations.RemoveField(
            model_name="node",
            name="object_id",
        ),
        migrations.RenameField(
            model_name="node",
            old_name="new_instance",
            new_name="instance",
        ),
    ]
