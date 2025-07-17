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
    def celery_tasks():
        return {
            "waldur_mastermind.marketplace_rancher.sync_managed_rancher_invoice_items": {
                "task": "waldur_mastermind.marketplace_rancher.sync_managed_rancher_invoice_items",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }
