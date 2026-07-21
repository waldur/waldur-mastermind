from waldur_core.core import WaldurExtension


class MarketplaceSiteAgentExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_site_agent"

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
            "waldur-create-offering-users-for-site-agent-offerings": {
                "task": "waldur_mastermind.marketplace_site_agent.sync_offering_users",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "mark-offering-backend-as-disconnected-after-timeout": {
                "task": "waldur_mastermind.marketplace_site_agent.mark_offering_backend_as_disconnected_after_timeout",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "sync-resources": {
                "task": "waldur_mastermind.marketplace_site_agent.sync_resources",
                "schedule": timedelta(minutes=10),
                "args": (),
            },
            "send-messages-about-pending-orders": {
                "task": "waldur_mastermind.marketplace_site_agent.send_messages_about_pending_orders",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "mark_agent_services_as_inactive": {
                "task": "waldur_mastermind.marketplace_site_agent.mark_agent_services_as_inactive",
                "schedule": timedelta(minutes=5),
                "args": (),
            },
            "delete-stale-event-subscriptions": {
                "task": "waldur_core.logging.delete_stale_event_subscriptions",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "delete-dangling-event-subscriptions": {
                "task": "waldur_core.logging.delete_dangling_event_subscriptions",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "cleanup-orphan-subscription-queues": {
                "task": "waldur_core.logging.cleanup_orphan_subscription_queues",
                "schedule": timedelta(hours=6),
                "args": (),
            },
            "cleanup-stale-agent-queues": {
                "task": "waldur_mastermind.marketplace_site_agent.cleanup_stale_agent_queues",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "cleanup-dangling-agent-queues": {
                "task": "waldur_mastermind.marketplace_site_agent.cleanup_dangling_agent_queues",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }
