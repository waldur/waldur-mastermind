from django.db import migrations

KEY = "openportal.project_usage_update"


def enable_project_usage_update_notification(apps, schema_editor):
    """Keep the project usage update flowing where it is in use.

    The update used to be mailed directly, bypassing the notification switch,
    to every project with an active OpenPortal allocation. It is now the
    ``openportal.project_usage_update`` notification, and notification rows
    are disabled by default, so turn it on wherever a project has an active
    allocation - the deployments that were already sending it.
    """
    Allocation = apps.get_model("waldur_openportal", "Allocation")
    if not Allocation.objects.filter(is_active=True, project__isnull=False).exists():
        return
    Notification = apps.get_model("core", "Notification")
    Notification.objects.update_or_create(key=KEY, defaults={"enabled": True})


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0059_rename_billing_mode_feature"),
        ("waldur_openportal", "0042_backfill_managedprojectattachment"),
    ]

    operations = [
        migrations.RunPython(
            enable_project_usage_update_notification, migrations.RunPython.noop
        ),
    ]
