import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("openstack_tenant", "0035_merge_subnet"),
        ("openstack", "0029_subnet_tenant"),
    ]

    operations = [
        migrations.AddField(
            model_name="port",
            name="instance",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ports",
                to="openstack_tenant.instance",
            ),
        ),
        migrations.AddField(
            model_name="port",
            name="subnet",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ports",
                to="openstack.subnet",
            ),
        ),
    ]
