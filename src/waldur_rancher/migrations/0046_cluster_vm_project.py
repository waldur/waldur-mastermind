import django.db.models.deletion
from django.db import migrations, models


def fill_vm_project(apps, schema_editor):
    Cluster = apps.get_model("waldur_rancher", "Cluster")

    for cluster in Cluster.objects.all():
        cluster.vm_project = cluster.project
        cluster.save(update_fields=["vm_project"])


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_rancher", "0045_add_new_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="cluster",
            name="vm_project",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="structure.project",
            ),
        ),
        migrations.RunPython(fill_vm_project),
        migrations.AlterField(
            model_name="cluster",
            name="vm_project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="structure.project"
            ),
        ),
    ]
