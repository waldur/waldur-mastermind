from waldur_core.core import WaldurExtension


class KeycloakExtension(WaldurExtension):
    """Stub extension kept solely so the waldur_keycloak app stays in
    INSTALLED_APPS for migration history.

    The marketplace-Keycloak group/membership feature was removed: group
    lifecycle now lives in the rancher-keycloak-operator (driven by
    waldur-site-agent writing ManagedRancherProject CRDs). The migration
    chain (0001-0005) must remain so deployments that already applied
    older migrations can run 0005_delete_models cleanly. After enough
    upgrade cycles have passed, this app + its migrations can be
    deleted entirely.
    """

    @staticmethod
    def django_app():
        return "waldur_keycloak"
