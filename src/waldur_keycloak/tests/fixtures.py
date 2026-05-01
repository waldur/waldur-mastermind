from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_keycloak.tests import factories
from waldur_mastermind.marketplace.enums import BASIC_OFFERING, OfferingStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class KeycloakFixture(structure_fixtures.ProjectFixture):
    def __init__(self):
        self.offering_role
        self.keycloak_group
        self.keycloak_membership

    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.customer,
            project=self.project,
            plugin_options={"keycloak_enabled": True},
            secret_options={
                "keycloak_url": "https://keycloak.example.com/auth/",
                "keycloak_realm": "waldur",
                "keycloak_user_realm": "master",
                "keycloak_username": "admin",
                "keycloak_password": "secret",
                "keycloak_ssl_verify": False,
            },
        )

    @cached_property
    def resource(self):
        return marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
        )

    @cached_property
    def offering_role(self):
        return factories.RoleFactory(
            name="Member",
        )

    @cached_property
    def keycloak_group(self):
        return factories.OfferingKeycloakGroupFactory(
            offering=self.offering,
            role=self.offering_role,
        )

    @cached_property
    def keycloak_membership(self):
        return factories.OfferingKeycloakMembershipFactory(
            group=self.keycloak_group,
            username="testuser",
            email="testuser@example.com",
        )
