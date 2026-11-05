"""Only who may change an offering reads its site agent's configuration and logs.

The rule the offering update route applies (OFFERING.UPDATE on the offering,
its organization or its service provider), as for an offering's
secret_options. The identity manager who created an identity also reads its
configuration. See #1073.
"""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    GlobalRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.permissions.utils import grant_global_role
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent.tests import factories

CONFIG = "waldur_api_token: secret"


class _Base(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()
        self.identity = factories.AgentIdentityFactory(
            offering=self.offering, config_file_content=CONFIG
        )
        self.log = factories.SiteAgentLogFactory(agent_identity=self.identity)

        # Callers who may change the offering.
        self.fixture.offering_owner  # the fixture grants OWNER OFFERING.UPDATE
        self.offering_manager = UserFactory()
        self.offering.add_user(self.offering_manager, OfferingRole.MANAGER)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.provider_manager = UserFactory()
        self.fixture.service_provider.add_user(
            self.provider_manager, ServiceProviderRole.MANAGER
        )
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Holds the offering role too; it may change the offering only while
        # the role carries OFFERING.UPDATE, which the *_without_update tests
        # take away.
        self.offering_reader = UserFactory()
        self.offering.add_user(self.offering_reader, OfferingRole.MANAGER)

    def user(self, name):
        return getattr(self, name, None) or getattr(self.fixture, name)

    def site_wide_offering_updater(self):
        role = GlobalRole.get("GLOBAL.OFFERING_DESK")
        role.add_permission(PermissionEnum.UPDATE_OFFERING)
        # OFFERING.LIST site-wide is what lists the identity row to them.
        role.add_permission(PermissionEnum.LIST_OFFERINGS)
        user = UserFactory()
        grant_global_role(user, role)
        return user


@ddt
class AgentConfigVisibilityTest(_Base):
    def config_as(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.AgentIdentityFactory.get_url(self.identity)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    @data("staff", "offering_owner", "offering_manager", "provider_manager")
    def test_who_may_change_the_offering_reads_the_config(self, name):
        body = self.config_as(self.user(name))
        self.assertEqual(body["config_file_content"], CONFIG)

    @data("global_support", "offering_support", "offering_admin")
    def test_who_may_not_change_the_offering_does_not(self, name):
        body = self.config_as(self.user(name))
        self.assertNotIn("config_file_content", body)
        self.assertEqual(body["uuid"], self.identity.uuid.hex)

    def test_offering_role_without_update_does_not(self):
        # Without OFFERING.UPDATE in the role, the offering role still shows
        # the offering, but no longer lets its holder change it.
        OfferingRole.MANAGER.delete_permission(PermissionEnum.UPDATE_OFFERING)
        body = self.config_as(self.offering_reader)
        self.assertNotIn("config_file_content", body)

    def test_site_wide_offering_update_reads_the_config(self):
        body = self.config_as(self.site_wide_offering_updater())
        self.assertEqual(body["config_file_content"], CONFIG)

    def test_list_decides_per_identity(self):
        # A second provider where the same caller only supports the
        # organization: its identity is listed, without the configuration.
        other = factories.AgentIdentityFactory(config_file_content="other secret")
        other.offering.type = enums.SITE_AGENT_OFFERING
        other.offering.save()
        other.offering.customer.add_user(self.offering_manager, CustomerRole.SUPPORT)

        self.client.force_authenticate(self.offering_manager)
        rows = self.client.get(factories.AgentIdentityFactory.get_list_url()).json()
        config = {row["uuid"]: row.get("config_file_content") for row in rows}
        self.assertEqual(config, {self.identity.uuid.hex: CONFIG, other.uuid.hex: None})

    def test_identity_manager_reads_the_config_of_their_own_identity(self):
        identity_manager = UserFactory(
            is_identity_manager=True, managed_isds=["isd:efp"]
        )
        own = factories.AgentIdentityFactory(
            offering=self.offering,
            config_file_content="own",
            created_by=identity_manager,
        )
        self.client.force_authenticate(identity_manager)
        rows = self.client.get(factories.AgentIdentityFactory.get_list_url()).json()
        self.assertEqual([row["uuid"] for row in rows], [own.uuid.hex])
        self.assertEqual(rows[0]["config_file_content"], "own")


@ddt
class AgentLogVisibilityTest(_Base):
    def logs_as(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.SiteAgentLogFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["uuid"] for row in response.json()]

    @data("staff", "offering_owner", "offering_manager", "provider_manager")
    def test_who_may_change_the_offering_reads_the_logs(self, name):
        self.assertEqual(self.logs_as(self.user(name)), [self.log.uuid.hex])

    @data("global_support", "offering_support", "offering_admin")
    def test_who_may_not_change_the_offering_does_not(self, name):
        self.assertEqual(self.logs_as(self.user(name)), [])

    def test_offering_role_without_update_does_not(self):
        OfferingRole.MANAGER.delete_permission(PermissionEnum.UPDATE_OFFERING)
        self.assertEqual(self.logs_as(self.offering_reader), [])

    def test_site_wide_offering_update_reads_the_logs(self):
        self.assertEqual(
            self.logs_as(self.site_wide_offering_updater()), [self.log.uuid.hex]
        )


class SiteAgentCallsTest(_Base):
    """What waldur-site-agent does at start-up, as the offering manager it runs as."""

    def test_agent_registers_updates_and_pushes_logs(self):
        self.client.force_authenticate(self.offering_manager)
        list_url = factories.AgentIdentityFactory.get_list_url()
        body = {
            "offering": self.offering.uuid.hex,
            "name": "agent-1",
            "version": "1.0.0",
            "config_file_path": "",
            "config_file_content": "",
        }
        created = self.client.post(list_url, body, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.json())

        found = self.client.get(list_url, {"name": "agent-1"}).json()
        self.assertEqual([row["uuid"] for row in found], [created.json()["uuid"]])

        updated = self.client.put(found[0]["url"], body, format="json")
        self.assertEqual(updated.status_code, status.HTTP_200_OK, updated.json())

        pushed = self.client.post(
            factories.SiteAgentLogFactory.get_list_url(),
            [
                {
                    "agent_identity_uuid": created.json()["uuid"],
                    "timestamp": 1_746_355_200.0,
                    "level": "INFO",
                    "message": "Agent started",
                    "module": "waldur_site_agent.main",
                }
            ],
            format="json",
        )
        self.assertEqual(pushed.status_code, status.HTTP_201_CREATED, pushed.json())
