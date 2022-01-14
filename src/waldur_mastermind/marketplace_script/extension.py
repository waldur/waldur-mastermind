from waldur_core.core import WaldurExtension


class MarketplaceScriptExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_script'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-marketplace-script-pull-resources': {
                'task': 'waldur_marketplace_script.pull_resources',
                'schedule': timedelta(hours=1),
                'args': (),
            },
        }
