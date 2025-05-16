import uuid

from django.db import migrations, models

import waldur_core.core.fields


def gen_uuid(apps, schema_editor):
    ClusterSecurityGroupRule = apps.get_model(
        "waldur_rancher", "ClusterSecurityGroupRule"
    )
    for row in ClusterSecurityGroupRule.objects.all():
        row.uuid = uuid.uuid4().hex
        row.save(update_fields=["uuid"])


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_rancher", "0048_clustersecuritygroup_clustersecuritygrouprule"),
    ]

    operations = [
        migrations.AddField(
            model_name="clustersecuritygrouprule",
            name="uuid",
            field=models.UUIDField(null=True),
        ),
        migrations.RunPython(gen_uuid, elidable=True),
        migrations.AlterField(
            model_name="clustersecuritygrouprule",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
