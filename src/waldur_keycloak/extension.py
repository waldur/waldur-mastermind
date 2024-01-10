from waldur_core.core import WaldurExtension


class KeycloakExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_keycloak"

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            "waldur-keycloak-sync-groups": {
                "task": "waldur_keycloak.sync_groups",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }
