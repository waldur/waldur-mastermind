from datetime import timedelta

from waldur_core.core import WaldurExtension


class AnalyticsExtension(WaldurExtension):
    class Settings:
        WALDUR_ANALYTICS = {
            'ENABLED': False,
            'DAILY_QUOTA_LIFETIME': timedelta(days=31),
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.analytics'

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
            'waldur-sync-daily-quotas': {
                'task': 'analytics.sync_daily_quotas',
                'schedule': timedelta(hours=24),
                'args': (),
            },
        }
