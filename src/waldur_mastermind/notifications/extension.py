from django.utils import timezone

from waldur_core.core import WaldurExtension


class NotificationsExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.notifications"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        return {
            "send-scheduled-broadcast-notifications": {
                "task": "waldur_mastermind.notifications.send_scheduled_broadcast_messages",
                "schedule": timezone.timedelta(hours=12),
                "args": (),
            },
        }
