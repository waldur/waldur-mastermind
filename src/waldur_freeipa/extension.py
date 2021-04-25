from waldur_core.core import WaldurExtension


class FreeIPAExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_freeipa'

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-freeipa-sync-groups': {
                'task': 'waldur_freeipa.sync_groups',
                'schedule': timedelta(minutes=10),
                'args': (),
            },
        }
