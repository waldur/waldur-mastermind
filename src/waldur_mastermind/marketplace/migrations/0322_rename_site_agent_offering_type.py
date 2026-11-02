from django.db import migrations

# Literals rather than enums: a migration must keep meaning what it meant
# when it was written, whatever the constants become later.
OLD_TYPE = "Marketplace.Slurm"
NEW_TYPE = "Marketplace.SiteAgent"
DISABLED_TYPES_KEY = "DISABLED_OFFERING_TYPES"


def _rename_offerings(apps, old, new):
    Offering = apps.get_model("marketplace", "Offering")
    Offering.objects.filter(type=old).update(type=new)


def _rename_disabled_type(apps, old, new):
    Setting = apps.get_model("configuration", "Setting")
    setting = Setting.objects.filter(key=DISABLED_TYPES_KEY).first()
    if setting is None or not isinstance(setting.value, list):
        return
    if old not in setting.value:
        return
    # dict.fromkeys keeps the order and collapses a list holding both names.
    setting.value = list(
        dict.fromkeys(new if value == old else value for value in setting.value)
    )
    setting.source = "migration"
    setting.source_detail = "marketplace.0322"
    setting.updated_by = None
    setting.save()


def forwards(apps, schema_editor):
    _rename_offerings(apps, OLD_TYPE, NEW_TYPE)
    _rename_disabled_type(apps, OLD_TYPE, NEW_TYPE)


def backwards(apps, schema_editor):
    _rename_offerings(apps, NEW_TYPE, OLD_TYPE)
    _rename_disabled_type(apps, NEW_TYPE, OLD_TYPE)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0321_backfill_resource_terminated_at"),
        # Settings are stored in configuration_setting from here on. The
        # constance table is left alone: it is kept for the previous release's
        # pods and for a rollback, and both expect the old name.
        ("configuration", "0002_import_constance_settings"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
