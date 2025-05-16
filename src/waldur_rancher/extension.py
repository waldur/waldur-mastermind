from waldur_core.core import WaldurExtension


class RancherExtension(WaldurExtension):
    class Settings:
        WALDUR_RANCHER = {
            "ROLE_REQUIREMENT": {
                "server": {"CPU": 2, "RAM": 4096},
                "agent": {"CPU": 1, "RAM": 1024},
            },
            # TODO: consider removing this
            "SYSTEM_VOLUME_MIN_SIZE": 64,
            "READ_ONLY_MODE": False,
            "DISABLE_AUTOMANAGEMENT_OF_USERS": False,
            "DISABLE_SSH_KEY_INJECTION": False,
            "DISABLE_DATA_VOLUME_CREATION": False,
        }

    @staticmethod
    def django_app():
        return "waldur_rancher"

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
            "waldur-rancher-update-clusters-nodes": {
                "task": "waldur_rancher.pull_all_clusters_nodes",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "waldur-rancher-sync-keycloak-users": {
                "task": "waldur_rancher.sync_keycloak_users",
                "schedule": timedelta(minutes=15),
                "args": (),
            },
            "waldur-rancher-sync-rancher-roles": {
                "task": "waldur_rancher.sync_rancher_roles",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "waldur-rancher-delete-leftover-keycloak-groups": {
                "task": "waldur_rancher.delete_leftover_keycloak_groups",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "waldur-rancher-delete-leftover-keycloak-memberships": {
                "task": "waldur_rancher.delete_leftover_keycloak_memberships",
                "schedule": timedelta(hours=1),
                "args": (),
            },
            "waldur-rancher-sync-rancher-group-bindings": {
                "task": "waldur_rancher.sync_rancher_group_bindings",
                "schedule": timedelta(hours=1),
                "args": (),
            },
        }

    @staticmethod
    def get_public_settings():
        return [
            "ROLE_REQUIREMENT",
            "SYSTEM_VOLUME_MIN_SIZE",
            "READ_ONLY_MODE",
            "DISABLE_SSH_KEY_INJECTION",
            "DISABLE_DATA_VOLUME_CREATION",
        ]
