from datetime import timedelta

from waldur_core.core import WaldurExtension


class MarketplaceExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace"

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
        from celery.schedules import crontab

        return {
            "pull-service-properties": {
                "task": "waldur_mastermind.marketplace.ServicePropertiesListPullTask",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "pull-service-resources": {
                "task": "waldur_mastermind.marketplace.ServiceResourcesListPullTask",
                # Pull resources strictly at the beginning of an hour
                "schedule": crontab(minute=0),
                "args": (),
            },
            "waldur-marketplace-calculate-usage": {
                "task": "waldur_mastermind.marketplace.calculate_usage_for_current_month",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "waldur-marketplace-sync-component-usage-summaries": {
                "task": "waldur_mastermind.marketplace.sync_component_usage_summaries",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "terminate_resources_if_project_end_date_has_been_reached": {
                "task": "waldur_mastermind.marketplace.terminate_resources_if_project_end_date_has_been_reached",
                # Set specific time 1:40 to avoid conflicts with regular pulls
                # and with the task terminate_expired_resources
                "schedule": crontab(minute=40, hour=1),
                "args": (),
            },
            "terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order": {
                "task": "waldur_mastermind.marketplace.terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "process_pending_start_date_orders": {
                "task": "waldur_mastermind.marketplace.process_pending_start_date_orders",
                "schedule": timedelta(hours=2),
                "args": (),
            },
            "process_pending_project_orders": {
                "task": "waldur_mastermind.marketplace.process_pending_project_orders",
                "schedule": timedelta(hours=2),
                "args": (),
            },
            "notification_about_project_ending": {
                "task": "waldur_mastermind.marketplace.notification_about_project_ending",
                "schedule": crontab(minute=0, hour=10),
                "args": (),
            },
            "notification_about_resource_ending": {
                "task": "waldur_mastermind.marketplace.notification_about_resource_ending",
                "schedule": crontab(minute=0, hour=10),
                "args": (),
            },
            "notify_about_stale_resource": {
                "task": "waldur_mastermind.marketplace.notify_about_stale_resource",
                "schedule": crontab(minute=0, hour=15, day_of_month="5"),
                "args": (),
            },
            "terminate_expired_resources": {
                "task": "waldur_mastermind.marketplace.terminate_expired_resources",
                # Set specific time 1:20 to avoid conflicts with regular pulls
                # and with the task terminate_resources_if_project_end_date_has_been_reached
                "schedule": crontab(minute=20, hour=1),
                "args": (),
            },
            "send_telemetry": {
                "task": "waldur_mastermind.marketplace.send_metrics",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "mark_resources_as_erred_after_timeout": {
                "task": "waldur_mastermind.marketplace.mark_resources_as_erred_after_timeout",
                "schedule": timedelta(hours=2),
                "args": (),
            },
            "remove_deleted_robot_accounts": {
                "task": "waldur_mastermind.marketplace.remove_deleted_robot_accounts",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "update_daily_consent_history": {
                "task": "waldur_mastermind.marketplace.update_daily_consent_history",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "revoke_outdated_consents": {
                "task": "waldur_mastermind.marketplace.revoke_outdated_consents",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "cleanup_stale_offering_users": {
                "task": "waldur_mastermind.marketplace.cleanup_stale_offering_users",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "reconcile_robot_account_access": {
                "task": "waldur_mastermind.marketplace.reconcile_robot_account_access",
                "schedule": crontab(minute=30, hour=2),  # Run daily at 2:30 AM
                "args": (),
            },
            "cleanup_usage_poll_records": {
                "task": "waldur_mastermind.marketplace.cleanup_usage_poll_records",
                "schedule": timedelta(days=1),
                "args": (),
            },
        }
