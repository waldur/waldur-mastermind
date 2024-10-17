import uuid
from base64 import b64decode
from pickle import loads

from django.db import migrations
from django.utils import timezone

import waldur_core.core.fields

SQL_QUERY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'binary_database_files_file') THEN
        WITH temp_file_list AS (
            SELECT DISTINCT ON (name) name FROM (
                SELECT image AS name FROM core_user WHERE image != ''
                UNION ALL
                SELECT image AS name FROM structure_customer WHERE image != ''
                UNION ALL
                SELECT image AS name FROM structure_project WHERE image != ''
                UNION ALL
                SELECT certificate AS name FROM structure_servicesettings WHERE certificate != ''
                UNION ALL
                SELECT pdf AS name FROM waldur_paypal_invoice WHERE pdf != ''
                UNION ALL
                SELECT icon AS name FROM waldur_rancher_template WHERE icon != ''
                UNION ALL
                SELECT file AS name FROM waldur_firecrest_job WHERE file != ''
                UNION ALL
                SELECT proof AS name FROM invoices_payment WHERE proof != ''
                UNION ALL
                SELECT image AS name FROM marketplace_serviceprovider WHERE image != ''
                UNION ALL
                SELECT icon AS name FROM marketplace_categorygroup WHERE icon != ''
                UNION ALL
                SELECT icon AS name FROM marketplace_category WHERE icon != ''
                UNION ALL
                SELECT image AS name FROM marketplace_offering WHERE image != ''
                UNION ALL
                SELECT thumbnail AS name FROM marketplace_offering WHERE thumbnail != ''
                UNION ALL
                SELECT image AS name FROM marketplace_screenshot WHERE image != ''
                UNION ALL
                SELECT thumbnail AS name FROM marketplace_screenshot WHERE thumbnail != ''
                UNION ALL
                SELECT file AS name FROM marketplace_offeringfile WHERE file != ''
                UNION ALL
                SELECT icon AS name FROM marketplace_checklist_category WHERE icon != ''
                UNION ALL
                SELECT image AS name FROM marketplace_checklist_question WHERE image != ''
                UNION ALL
                SELECT file AS name FROM proposal_calldocument WHERE file != ''
                UNION ALL
                SELECT image AS name FROM proposal_callmanagingorganisation WHERE image != ''
                UNION ALL
                SELECT file AS name FROM proposal_proposaldocumentation WHERE file != ''
                UNION ALL
                SELECT file AS name FROM support_attachment WHERE file != ''
                UNION ALL
                SELECT thumbnail AS name FROM support_attachment WHERE thumbnail != ''
                UNION ALL
                SELECT file AS name FROM support_templateattachment WHERE file != ''
            ) AS combined_files
        )
        INSERT INTO media_file (name, content, size, created, modified, mime_type, is_public)
        SELECT DISTINCT ON (bdf.name)
            bdf.name,
            bdf.content,
            bdf.size,
            bdf.created_datetime AS created,
            bdf.created_datetime AS modified,
            'application/octet-stream' AS mime_type,
            false AS is_public
        FROM
            binary_database_files_file bdf
        JOIN
            temp_file_list tfl ON bdf.name = tfl.name
        ORDER BY
            bdf.name, bdf.created_datetime DESC;

        DROP TABLE IF EXISTS binary_database_files_file;
    END IF;
END $$;
"""


ICONS = (
    "SITE_LOGO",
    "SIDEBAR_LOGO",
    "SIDEBAR_LOGO_MOBILE",
    "SIDEBAR_LOGO_DARK",
    "POWERED_BY_LOGO",
    "HERO_IMAGE",
    "LOGIN_LOGO",
    "FAVICON",
    "OFFERING_LOGO_PLACEHOLDER",
)


def copy_whitelabeling(apps, schema_editor):
    File = apps.get_model("media", "File")

    with schema_editor.connection.cursor() as cursor:
        for param in ICONS:
            cursor.execute("SELECT value FROM constance_config WHERE key=%s", [param])
            row = cursor.fetchone()
            filename = loads(b64decode(row[0].encode()))
            cursor.execute(
                "SELECT content FROM binary_database_files_file WHERE name=%s",
                [filename],
            )
            row = cursor.fetchone()
            content = row[0]
            File.objects.create(
                name=filename,
                content=content,
                size=len(content),
                created=timezone.now(),
                modified=timezone.now(),
                mime_type="application/octet-stream",
                is_public=False,
            )


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
        migrations.RunPython(copy_whitelabeling),
        migrations.AlterField(
            model_name="file",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(null=True, blank=True),
        ),
        migrations.RunSQL(SQL_QUERY),
        migrations.RunPython(gen_uuid, elidable=True),
        migrations.AlterField(
            model_name="file",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
