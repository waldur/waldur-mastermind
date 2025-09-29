import json

from django.core.management.base import BaseCommand
from django.db import IntegrityError

from waldur_core.core.models import Notification, NotificationTemplate
from waldur_core.structure.notifications import NOTIFICATIONS


def check_notification_existence(notification_key):
    for key, section in NOTIFICATIONS.items():
        for notification in section:
            if notification_key == f"{key}.{notification['path']}":
                return True
    return False


class Command(BaseCommand):
    help = "Import notifications to DB"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "notifications_file",
            help="Specifies location of notifications file.",
        )

    def handle(self, *args, **options):
        with open(options["notifications_file"]) as notifications_file:
            notifications = json.load(notifications_file)

        valid_notifications_data = []
        for notification_from_file in notifications:
            if not check_notification_existence(notification_from_file):
                self.stdout.write(
                    self.style.WARNING(
                        f"Invalid notifications detected: {notification_from_file}"
                    )
                )
        for key, section in NOTIFICATIONS.items():
            for notification in section:
                path = f"{key}.{notification['path']}"
                if check_notification_existence(path):
                    notification_data = {
                        "path": path,
                        "templates": {
                            f"{key}/{template.path}": template.name
                            for template in notification["templates"]
                        },
                        "description": notification.get("description"),
                    }
                    valid_notifications_data.append(notification_data)

        for valid_notification_data in valid_notifications_data:
            try:
                notification, created = Notification.objects.get_or_create(
                    key=valid_notification_data["path"],
                )
                for (
                    notification_template_path,
                    template_name,
                ) in valid_notification_data["templates"].items():
                    try:
                        # Use defaults to avoid MultipleObjectsReturned
                        created_notification_template, _ = (
                            NotificationTemplate.objects.get_or_create(
                                path=notification_template_path,
                                defaults={"name": template_name},
                            )
                        )
                        notification.templates.add(created_notification_template)
                    except NotificationTemplate.MultipleObjectsReturned:
                        # Handle case where multiple templates exist with same path
                        self.stdout.write(
                            self.style.ERROR(
                                f"Multiple NotificationTemplate objects found for path '{notification_template_path}', "
                                f"using first one for notification '{valid_notification_data['path']}'"
                            )
                        )
                        # Use the first existing template
                        existing_template = NotificationTemplate.objects.filter(
                            path=notification_template_path
                        ).first()
                        if existing_template:
                            notification.templates.add(existing_template)
                    except IntegrityError as e:
                        self.stdout.write(
                            self.style.ERROR(
                                f"Database integrity error for template '{notification_template_path}': {str(e)}, skipping"
                            )
                        )
                        continue
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(
                                f"Error processing template '{notification_template_path}': {str(e)}, skipping"
                            )
                        )
                        continue

                notification.description = valid_notification_data.get("description")
                notification.save()

                file_enabled_status = notifications.get(
                    valid_notification_data.get("path")
                )
                if (
                    file_enabled_status is not None
                    and notification.enabled != file_enabled_status
                ):
                    notification.enabled = file_enabled_status
                    notification.save()
                    self.stdout.write(
                        self.style.WARNING(
                            f"The notification {notification.key} status has been changed to {notification.enabled}"
                        )
                    )
                if created:
                    self.stdout.write(
                        self.style.WARNING(
                            f"The notification {notification.key} has been created with status {notification.enabled}"
                        )
                    )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"Failed to process notification '{valid_notification_data['path']}': {str(e)}, skipping"
                    )
                )
                continue
