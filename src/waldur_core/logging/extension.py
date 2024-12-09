from waldur_core.core import WaldurExtension


class LoggingExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_core.logging"

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            "delete-stale-event-subscriptions": {
                "task": "waldur_core.logging.delete_stale_event_subscriptions",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "delete-dangling-event-subscriptions": {
                "task": "waldur_core.logging.delete_dangling_event_subscriptions",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }
