from datetime import timedelta

from waldur_core.core import WaldurExtension


class MarketplaceExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from celery.schedules import crontab

        return {
            'waldur-marketplace-calculate-usage': {
                'task': 'waldur_mastermind.marketplace.calculate_usage_for_current_month',
                'schedule': timedelta(hours=1),
                'args': (),
            },
            'waldur-mastermind-send-notifications-about-usages': {
                'task': 'waldur_mastermind.marketplace.send_notifications_about_usages',
                'schedule': crontab(minute=0, hour=10, day_of_month='25'),
                'args': (),
            },
            'terminate_resources_if_project_end_date_has_been_reached': {
                'task': 'waldur_mastermind.marketplace.terminate_resources_if_project_end_date_has_been_reached',
                'schedule': timedelta(days=1),
                'args': (),
            },
            'notification_about_project_ending': {
                'task': 'waldur_mastermind.marketplace.notification_about_project_ending',
                'schedule': crontab(minute=0, hour=10),
                'args': (),
            },
            'notify_about_stale_resource': {
                'task': 'waldur_mastermind.marketplace.notify_about_stale_resource',
                'schedule': crontab(minute=0, hour=15, day_of_month='5'),
                'args': (),
            },
            'terminate_expired_resources': {
                'task': 'waldur_mastermind.marketplace.terminate_expired_resources',
                'schedule': timedelta(days=1),
                'args': (),
            },
            'send_telemetry': {
                'task': 'waldur_mastermind.marketplace.send_metrics',
                'schedule': timedelta(days=1),
                'args': (),
            },
        }
