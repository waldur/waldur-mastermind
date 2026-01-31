from datetime import timedelta

from waldur_core.core import WaldurExtension


class ArrowExtension(WaldurExtension):
    """Waldur extension for Arrow (ArrowSphere) billing integration."""

    @staticmethod
    def django_app():
        return "waldur_mastermind.waldur_arrow"

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        # Use default sync intervals - dynamic config is checked in the task itself
        return {
            "sync-arrow-billing": {
                "task": "waldur_mastermind.waldur_arrow.sync_arrow_billing_scheduled",
                "schedule": timedelta(hours=6),
                "args": (),
            },
            "check-arrow-validated-billing": {
                "task": "waldur_mastermind.waldur_arrow.check_validated_billing",
                "schedule": timedelta(hours=12),
                "args": (),
            },
            "sync-arrow-consumption": {
                "task": "waldur_mastermind.waldur_arrow.sync_arrow_consumption_scheduled",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "check-arrow-billing-export": {
                "task": "waldur_mastermind.waldur_arrow.check_billing_export_scheduled",
                "schedule": timedelta(hours=6),
                "args": (),
            },
        }
