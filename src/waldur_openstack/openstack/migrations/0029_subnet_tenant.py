import django.db.models.deletion
from django.db import migrations, models


def fill_tenant(apps, schema_editor):
    SubNet = apps.get_model("openstack", "SubNet")

    for subnet in SubNet.objects.all():
        subnet.tenant = subnet.network.tenant
        subnet.save(update_fields=["tenant"])


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0001_squashed_0028"),
    ]

    operations = [
        migrations.AddField(
            model_name="subnet",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="openstack.tenant",
            ),
        ),
        migrations.RunPython(fill_tenant),
        migrations.AlterField(
            model_name="subnet",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="openstack.tenant",
            ),
        ),
    ]
