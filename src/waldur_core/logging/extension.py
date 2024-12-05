from waldur_core.core import WaldurExtension


class LoggingExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_core.logging"

    @staticmethod
    def celery_tasks():
        from celery.schedules import crontab

        return {
            "delete-stale-event-subscriptions": {
                "task": "waldur_core.logging.delete_stale_event_subscriptions",
                "schedule": crontab(hours=24),
                "args": (),
            },
        }
