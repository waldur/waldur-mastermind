from waldur_core.core import WaldurExtension


class MarketplaceScriptExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_script"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            "waldur-marketplace-script-pull-resources": {
                "task": "waldur_marketplace_script.pull_resources",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "waldur-marketplace-script-remove-old-dry-runs": {
                "task": "waldur_marketplace_script.remove_old_dry_runs",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "marketplace_script.mark_terminating_resources_as_erred_after_timeout": {
                "task": "waldur_mastermind.marketplace_script.mark_terminating_resources_as_erred_after_timeout",
                "schedule": timedelta(hours=2),
                "args": (),
            },
        }
