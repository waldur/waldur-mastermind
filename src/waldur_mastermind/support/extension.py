from datetime import timedelta

from waldur_core.core import WaldurExtension


class SupportExtension(WaldurExtension):
    class Settings:
        SUPPRESS_NOTIFICATION_EMAILS = False
        ISSUE_FEEDBACK_ENABLE = False
        # Measured in days
        ISSUE_FEEDBACK_TOKEN_PERIOD = 7

    @staticmethod
    def django_app():
        return 'waldur_mastermind.support'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        return {
            'pull-support-users': {
                'task': 'waldur_mastermind.support.pull_support_users',
                'schedule': timedelta(hours=6),
                'args': (),
            },
            'pull-priorities': {
                'task': 'waldur_mastermind.support.pull_priorities',
                'schedule': timedelta(hours=24),
                'args': (),
            },
            'sync_request_types': {
                'task': 'waldur_mastermind.support.sync_request_types',
                'schedule': timedelta(hours=24),
                'args': (),
            },
            'run_periodic_task': {
                'task': 'waldur_mastermind.support.run_periodic_task',
                'schedule': timedelta(hours=6),
                'args': (),
            },
        }
