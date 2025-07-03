from waldur_core.core import WaldurExtension


class MarketplaceSiteAgentExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_site_agent"

    @staticmethod
    def is_assembly():
        return True

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
        }
