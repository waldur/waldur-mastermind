import json

from django.core.management.base import BaseCommand

from waldur_core.core.models import Notification
from waldur_core.structure.notifications import NOTIFICATIONS


class Command(BaseCommand):
    help = "Import notifications to DB"

    def add_arguments(self, parser):
        super(Command, self).add_arguments(parser)
        parser.add_argument(
            'notifications_file',
            help='Specifies location of notifications file.',
        )

    def handle(self, *args, **options):

        with open(options['notifications_file'], 'r') as notifications_file:
            notifications = json.load(notifications_file)

        valid_notifications_data = []

        for section in NOTIFICATIONS:
            for notification in section['items']:
                path = f"{section['key']}.{notification['path']}"
                if path in notifications:
                    notification_data = {
                        "path": path,
                        "templates": {
                            f"{section['key']}/{template.path}": template.name
                            for template in notification['templates']
                        },
                    }
                    valid_notifications_data.append(notification_data)
                else:
                    self.stdout.write(
                        self.style.WARNING(f'Invalid notifications detected: {path}')
                    )

        for valid_notification_data in valid_notifications_data:
            notification, created = Notification.objects.get_or_create(
                key=valid_notification_data['path'],
            )
            file_enabled_status = notifications[valid_notification_data['path']]
            if notification.enabled != file_enabled_status:
                notification.enabled = notifications[valid_notification_data['path']]
                notification.save()
                self.stdout.write(
                    self.style.WARNING(
                        f'The notification {notification.key} status has been changed to {notification.enabled}'
                    )
                )
            if created:
                self.stdout.write(
                    self.style.WARNING(
                        f'The notification {notification.key} has been created with status {notification.enabled}'
                    )
                )
