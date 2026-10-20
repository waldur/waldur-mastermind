from django.db import migrations

from waldur_core.configuration.migration_utils import CONSTANCE_TABLE, table_exists


def remove_site_logo_setting(apps, schema_editor):
    if not table_exists(schema_editor.connection, CONSTANCE_TABLE):
        return
    schema_editor.execute("DELETE FROM constance_constance WHERE key = 'SITE_LOGO'")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_user_identity_bridge_fields"),
    ]

    operations = [
        migrations.RunPython(remove_site_logo_setting, migrations.RunPython.noop),
    ]
