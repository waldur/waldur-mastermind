import logging

from django.db import migrations

logger = logging.getLogger(__name__)

SOURCE_KEY = "users.invitation_created"

NEW_KEYS = [
    "users.call_invitation_created",
    "users.proposal_invitation_created",
]


def create_invitation_notifications(apps, schema_editor):
    """Split call and proposal invitations off the shared invitation notification.

    Both used to be delivered under ``users.invitation_created``, so the new keys
    inherit its enabled status: notifications default to disabled, and creating
    them as such would silently stop the emails on deployments which had them on.
    """
    Notification = apps.get_model("core", "Notification")

    source = Notification.objects.filter(key=SOURCE_KEY).first()
    enabled = source.enabled if source else False

    for key in NEW_KEYS:
        _, created = Notification.objects.get_or_create(
            key=key, defaults={"enabled": enabled}
        )
        if created:
            logger.info("Created notification '%s' (enabled=%s)", key, enabled)


def delete_invitation_notifications(apps, schema_editor):
    Notification = apps.get_model("core", "Notification")
    Notification.objects.filter(key__in=NEW_KEYS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0041_backfill_user_initial_revisions"),
    ]

    operations = [
        migrations.RunPython(
            create_invitation_notifications,
            reverse_code=delete_invitation_notifications,
        ),
    ]
