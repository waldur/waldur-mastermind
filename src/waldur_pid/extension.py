from datetime import timedelta

from waldur_core.core import WaldurExtension


class PIDExtension(WaldurExtension):
    class Settings:
        WALDUR_PID = {
            'DATACITE': {
                'REPOSITORY_ID': '',
                'PASSWORD': '',
                'PREFIX': '',
                'API_URL': 'https://example.com',
                'PUBLISHER': 'Waldur',
                'COLLECTION_DOI': '',
            }
        }

    @staticmethod
    def django_app():
        return 'waldur_pid'

    @staticmethod
    def celery_tasks():

        return {
            'waldur-pid-update-all-referrables': {
                'task': 'waldur_pid.update_all_referrables',
                'schedule': timedelta(days=1),
                'args': (),
            },
        }
