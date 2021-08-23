from waldur_core.core import WaldurExtension


class KeycloakRancherExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_keycloak_rancher'

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-keycloak-rancher-sync-groups': {
                'task': 'waldur_keycloak_rancher.sync_groups',
                'schedule': timedelta(hours=1),
                'args': (),
            },
        }
