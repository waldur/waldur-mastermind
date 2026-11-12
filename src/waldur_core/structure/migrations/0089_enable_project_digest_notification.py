from django.db import migrations

KEY = "structure.project_digest"


def enable_project_digest_notification(apps, schema_editor):
    """Keep digests flowing where organizations already use them.

    The digest used to bypass the notification switch, so a deployment
    could send digests with ``structure.project_digest`` disabled (the
    default for notification rows). Now that the switch is honoured, turn it
    on wherever an organization has the digest enabled.
    """
    ProjectDigestConfiguration = apps.get_model(
        "structure", "ProjectDigestConfiguration"
    )
    if not ProjectDigestConfiguration.objects.filter(is_enabled=True).exists():
        return
    Notification = apps.get_model("core", "Notification")
    Notification.objects.update_or_create(key=KEY, defaults={"enabled": True})


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0055_versionupdatenotification"),
        ("structure", "0088_backfill_project_initial_revisions"),
    ]

    operations = [
        migrations.RunPython(
            enable_project_digest_notification, migrations.RunPython.noop
        ),
    ]
