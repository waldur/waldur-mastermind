import uuid

from django.db import migrations

import waldur_core.core.fields


def gen_uuid(apps, schema_editor):
    File = apps.get_model("media", "File")
    for row in File.objects.all():
        row.uuid = uuid.uuid4().hex
        row.save(update_fields=["uuid"])


class Migration(migrations.Migration):
    dependencies = [
        ("media", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="file",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(null=True, blank=True),
        ),
        migrations.RunPython(gen_uuid, elidable=True),
        migrations.AlterField(
            model_name="file",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
