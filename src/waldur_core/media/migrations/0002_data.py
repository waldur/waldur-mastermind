import uuid

from django.db import migrations

import waldur_core.core.fields

SQL_QUERY = """
DO $$
BEGIN
    IF to_regclass('public.binary_database_files_file') IS NOT NULL THEN
        INSERT INTO media_file (name, content, size, created, modified, mime_type, is_public)
        SELECT
            name,
            content,
            size,
            created_datetime AS created,
            created_datetime AS modified,
            'application/octet-stream' AS mime_type,
            false AS is_public
        FROM
            binary_database_files_file;
        DROP TABLE binary_database_files_file;
    END IF;
END $$;
"""


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
            model_name="media_file",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(null=True, blank=True),
        ),
        migrations.RunSQL(SQL_QUERY),
        migrations.RunPython(gen_uuid, elidable=True),
        migrations.AlterField(
            model_name="media_file",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
