from . import views


def register_in(router):
    router.register(
        r"offering-keycloak-groups",
        views.OfferingKeycloakGroupViewSet,
        basename="offering-keycloak-group",
    )
    router.register(
        r"offering-keycloak-memberships",
        views.OfferingKeycloakMembershipViewSet,
        basename="offering-keycloak-membership",
    )
