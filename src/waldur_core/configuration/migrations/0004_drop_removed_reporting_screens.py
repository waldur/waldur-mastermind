"""Forget the proposal reporting screens that no longer exist.

Call performance, review progress and resource demand were replaced by the
reports in call management. ENABLED_REPORTING_SCREENS only accepts the
screens there are, so a stored value still naming them could not be saved
back as it was read.
"""

from django.db import migrations

KEY = "ENABLED_REPORTING_SCREENS"
REMOVED = {"call-performance", "review-progress", "resource-demand"}


def drop_removed_screens(apps, schema_editor):
    Setting = apps.get_model("configuration", "Setting")
    for setting in Setting.objects.filter(key=KEY):
        if not isinstance(setting.value, list):
            continue
        kept = [screen for screen in setting.value if screen not in REMOVED]
        if kept != setting.value:
            setting.value = kept
            setting.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [
        ("configuration", "0003_setting_locked"),
    ]

    operations = [
        migrations.RunPython(drop_removed_screens, migrations.RunPython.noop),
    ]
