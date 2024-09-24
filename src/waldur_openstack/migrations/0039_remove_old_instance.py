import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0038_copy_resources"),
    ]

    operations = [
        migrations.AlterField(
            model_name="port",
            name="instance",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ports",
                to="openstack.instance",
            ),
        ),
        migrations.AddField(
            model_name="instance",
            name="subnets",
            field=models.ManyToManyField(
                through="openstack.Port", to="openstack.subnet"
            ),
        ),
    ]
