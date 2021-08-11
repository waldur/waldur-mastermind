from waldur_core.core import WaldurExtension


class FirecrestExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_firecrest'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-firecrest-pull-jobs': {
                'task': 'waldur_firecrest.pull_jobs',
                'schedule': timedelta(hours=1),
                'args': (),
            },
        }
