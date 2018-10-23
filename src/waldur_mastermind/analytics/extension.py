from __future__ import unicode_literals

from datetime import timedelta

from waldur_core.core import WaldurExtension


class AnalyticsExtension(WaldurExtension):
    class Settings:
        # See also: http://influxdb-python.readthedocs.io/en/latest/api-documentation.html#influxdbclient
        WALDUR_ANALYTICS = {
            'ENABLED': False,
            'INFLUXDB': {
                'host': 'localhost',
                'port': 8086,
                'username': 'USERNAME',
                'password': 'PASSWORD',
                'database': 'DATABASE',
                'ssl': False,
                'verify_ssl': False,
            }
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
            'waldur-push-analytics': {
                'task': 'analytics.push_points',
                'schedule': timedelta(minutes=30),
                'args': (),
            },
            'waldur-sync-daily-quotas': {
                'task': 'analytics.sync_daily_quotas',
                'schedule': timedelta(hours=24),
                'args': (),
            },
        }
