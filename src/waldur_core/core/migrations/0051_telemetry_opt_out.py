from django.db import migrations

LEGACY_KEY = "telemetry.send_metrics"
CURRENT_KEY = "deployment.send_metrics"


def carry_over_legacy_telemetry_opt_out(apps, schema_editor):
    # The flag moved from the "telemetry" section to "deployment", and rows
    # written before the move were left behind. Telemetry is on unless an
    # admin switched it off, so only an explicit "off" needs carrying over;
    # the legacy row is then dropped so it cannot be mistaken for the live
    # setting again.
    Feature = apps.get_model("core", "Feature")
    legacy = Feature.objects.filter(key=LEGACY_KEY).first()
    if legacy is None:
        return
    if not legacy.value:
        Feature.objects.get_or_create(key=CURRENT_KEY, defaults={"value": False})
    legacy.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0050_notificationtemplate_path_unique"),
    ]

    operations = [
        migrations.RunPython(
            carry_over_legacy_telemetry_opt_out, reverse_code=migrations.RunPython.noop
        ),
    ]
