from django.db import migrations


def remove_site_logo_setting(apps, schema_editor):
    schema_editor.execute("DELETE FROM constance_constance WHERE key = 'SITE_LOGO'")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_user_identity_bridge_fields"),
    ]

    operations = [
        migrations.RunPython(remove_site_logo_setting, migrations.RunPython.noop),
    ]
