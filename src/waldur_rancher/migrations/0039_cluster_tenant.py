import django.db.models.deletion
from django.db import migrations, models


def fill_tenant(apps, schema_editor):
    Cluster = apps.get_model("waldur_rancher", "Cluster")

    for cluster in Cluster.objects.exclude(tenant_settings__isnull=True):
        cluster.tenant_id = cluster.tenant_settings.object_id
        cluster.save(update_fields=["tenant_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0039_remove_old_instance"),
        ("waldur_rancher", "0038_alter_rancheruser_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="cluster",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="openstack.tenant",
            ),
        ),
        migrations.RunPython(fill_tenant),
        migrations.RemoveField(
            model_name="cluster",
            name="tenant_settings",
        ),
    ]
