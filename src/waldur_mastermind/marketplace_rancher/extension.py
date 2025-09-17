from datetime import timedelta

from waldur_core.core import WaldurExtension


class MarketplaceRancherExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_rancher"

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
            "waldur_mastermind.marketplace_rancher.report_rancher_usage": {
                "task": "waldur_mastermind.marketplace_rancher.report_rancher_usage",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }
