from django.db import migrations

SQL_QUERY = """
DO $$
BEGIN
    IF to_regclass('public.binary_database_files_file') IS NOT NULL THEN
        INSERT INTO media_file (name, content, size, created, modified, uuid, mime_type, is_public)
        SELECT
            name,
            content,
            size,
            created_datetime AS created,
            created_datetime AS modified,
            gen_random_uuid() AS uuid,
            'application/octet-stream' AS mime_type,
            false AS is_public
        FROM
            binary_database_files_file;
        DROP TABLE binary_database_files_file;
    END IF;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("media", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(SQL_QUERY),
    ]
