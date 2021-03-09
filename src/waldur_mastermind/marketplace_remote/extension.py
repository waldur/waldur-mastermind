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
            'waldur-remote-pull-order-items': {
                'task': 'waldur_mastermind.marketplace_remote.pull_order_items',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
            'waldur-remote-pull-invoices': {
                'task': 'waldur_mastermind.marketplace_remote.pull_invoices',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
        }
