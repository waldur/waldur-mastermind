import logging

from django.db import migrations

logger = logging.getLogger(__name__)

NOTIFICATION_KEYS = [
    "marketplace.notification_quota_full",
    "marketplace.notification_quota_75_percent",
]

TEMPLATE_PATHS = [
    "marketplace/notification_quota_full_subject.txt",
    "marketplace/notification_quota_full_message.txt",
    "marketplace/notification_quota_full_message.html",
    "marketplace/notification_quota_75_percent_subject.txt",
    "marketplace/notification_quota_75_percent_message.txt",
    "marketplace/notification_quota_75_percent_message.html",
]


def remove_quota_notifications(apps, schema_editor):
    Notification = apps.get_model("core", "Notification")
    NotificationTemplate = apps.get_model("core", "NotificationTemplate")

    notifications = Notification.objects.filter(key__in=NOTIFICATION_KEYS)
    logger.info("Deleting %s quota notification(s)", notifications.count())
    notifications.delete()

    templates = NotificationTemplate.objects.filter(path__in=TEMPLATE_PATHS)
    logger.info("Deleting %s quota notification template(s)", templates.count())
    templates.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_user_organization_address"),
    ]

    operations = [
        migrations.RunPython(
            remove_quota_notifications,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
