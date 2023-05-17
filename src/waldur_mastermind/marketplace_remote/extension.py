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
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-remote-pull-offerings': {
                'task': 'waldur_mastermind.marketplace_remote.pull_offerings',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
            'waldur-remote-pull-offering-users': {
                'task': 'waldur_mastermind.marketplace_remote.pull_offering_users',
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
            'waldur-remote-pull-invoices': {
                'task': 'waldur_mastermind.marketplace_remote.pull_invoices',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
            'waldur-remote-pull-robot-accounts': {
                'task': 'waldur_mastermind.marketplace_remote.pull_robot_accounts',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
            'waldur-remote-notify-about-pending-project-update-requests': {
                'task': 'waldur_mastermind.marketplace_remote.notify_about_pending_project_update_requests',
                'schedule': timedelta(weeks=1),
                'args': (),
            },
            'waldur-remote-push-project-data': {
                'task': 'waldur_mastermind.marketplace_remote.push_remote_project_data',
                'schedule': timedelta(days=1),
                'args': (),
            },
        }
