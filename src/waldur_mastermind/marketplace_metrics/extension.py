from waldur_core.core import WaldurExtension


class MarketplaceMetricsExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_metrics"

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
            "marketplace-metrics-roll-up": {
                "task": "waldur_mastermind.marketplace_metrics.roll_up",
                "schedule": timedelta(minutes=15),
                "args": (),
            },
            "marketplace-metrics-enforce-retention": {
                "task": "waldur_mastermind.marketplace_metrics.enforce_retention",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "marketplace-metrics-purge-archived": {
                "task": "waldur_mastermind.marketplace_metrics.purge_archived",
                "schedule": timedelta(days=1),
                "args": (),
            },
        }
