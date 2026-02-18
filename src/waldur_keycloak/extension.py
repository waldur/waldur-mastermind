from waldur_core.core import WaldurExtension


class KeycloakExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_keycloak"

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            "waldur-keycloak-sync-pending-memberships": {
                "task": "waldur_keycloak.sync_pending_memberships",
                "schedule": timedelta(minutes=15),
                "args": (),
            },
            "waldur-keycloak-cleanup-orphaned-groups": {
                "task": "waldur_keycloak.cleanup_orphaned_groups",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "waldur-keycloak-cleanup-orphaned-memberships": {
                "task": "waldur_keycloak.cleanup_orphaned_memberships",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }
