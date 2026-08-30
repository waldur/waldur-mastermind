from django.db import migrations

KEY = "proposal.workflow_step_event"


def create_notification(apps, schema_editor):
    """Register the workflow-step-event notification, enabled.

    Unlike broadcast notifications, this one only fires for rules a call
    manager configured (or the per-call seeded defaults), so the opt-in
    already happened at the call level; creating it disabled would silently
    swallow every configured rule.
    """
    Notification = apps.get_model("core", "Notification")
    if not Notification.objects.exists():
        # Fresh install: ``load_notifications`` creates the row with the
        # operator-chosen enabled state, and ``migrate_fresh`` skips this
        # migration entirely - seeding here would make the two paths diverge.
        return
    Notification.objects.get_or_create(key=KEY, defaults={"enabled": True})


def delete_notification(apps, schema_editor):
    Notification = apps.get_model("core", "Notification")
    Notification.objects.filter(key=KEY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0045_remove_access_request_notification"),
    ]

    operations = [
        migrations.RunPython(create_notification, reverse_code=delete_notification),
    ]
