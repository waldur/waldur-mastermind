from waldur_core.core import WaldurExtension


class MarketplaceRemoteExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_remote'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-remote-pull-offerings': {
                'task': 'waldur_mastermind.marketplace_remote.pull_offerings',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
            'waldur-remote-pull-resources': {
                'task': 'waldur_mastermind.marketplace_remote.pull_resources',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
            'waldur-remote-pull-order-items': {
                'task': 'waldur_mastermind.marketplace_remote.pull_order_items',
                'schedule': timedelta(minutes=5),
                'args': (),
            },
            'waldur-remote-pull-usage': {
                'task': 'waldur_mastermind.marketplace_remote.pull_usage',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
            'waldur-remote-sync-remote-project-permissions': {
                'task': 'waldur_mastermind.marketplace_remote.sync_remote_project_permissions',
                'schedule': timedelta(hours=6),
                'args': (),
            },
            'waldur-remote-sync-remote-projects': {
                'task': 'waldur_mastermind.marketplace_remote.sync_remote_projects',
                'schedule': timedelta(hours=6),
                'args': (),
            },
            'waldur-remote-pull-invoices': {
                'task': 'waldur_mastermind.marketplace_remote.pull_invoices',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
        }
