from django.db import migrations, models


def fill_new_role(apps, schema_editor):
    Node = apps.get_model("waldur_rancher", "Node")
    ClusterTemplateNode = apps.get_model("waldur_rancher", "clustertemplatenode")

    for model in (Node, ClusterTemplateNode):
        for node in model.objects.all():
            node.role = (
                "server" if node.controlplane_role or node.etcd_role else "agent"
            )
            node.save(update_fields=["role"])


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_rancher", "0044_keycloakusergroupmembership_first_name_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="clustertemplatenode",
            name="role",
            field=models.CharField(
                max_length=10,
                blank=True,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="node",
            name="role",
            field=models.CharField(
                max_length=10,
                blank=True,
                null=True,
            ),
        ),
        migrations.RunPython(fill_new_role),
        migrations.AlterField(
            model_name="clustertemplatenode",
            name="role",
            field=models.CharField(
                choices=[("agent", "agent"), ("server", "server")],
                db_index=True,
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="node",
            name="role",
            field=models.CharField(
                choices=[("agent", "agent"), ("server", "server")],
                db_index=True,
                max_length=10,
            ),
        ),
        migrations.RemoveField(
            model_name="clustertemplatenode",
            name="controlplane_role",
        ),
        migrations.RemoveField(
            model_name="clustertemplatenode",
            name="etcd_role",
        ),
        migrations.RemoveField(
            model_name="clustertemplatenode",
            name="worker_role",
        ),
        migrations.RemoveField(
            model_name="node",
            name="controlplane_role",
        ),
        migrations.RemoveField(
            model_name="node",
            name="etcd_role",
        ),
        migrations.RemoveField(
            model_name="node",
            name="worker_role",
        ),
    ]
