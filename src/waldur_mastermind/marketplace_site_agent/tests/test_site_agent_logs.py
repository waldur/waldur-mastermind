"""Tests for the marketplace-site-agent-logs API endpoint."""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent import models
from waldur_mastermind.marketplace_site_agent.tests import factories


def _make_payload(agent_identity, entries=None):
    if entries is None:
        entries = [
            {
                "agent_identity_uuid": agent_identity.uuid.hex,
                "timestamp": 1_746_355_200.0,
                "level": "INFO",
                "message": "Agent started",
                "module": "waldur_site_agent.main",
            }
        ]
    return entries


@ddt
class SiteAgentLogCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = marketplace_enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_identity = factories.AgentIdentityFactory(offering=self.offering)

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

        self.url = factories.SiteAgentLogFactory.get_list_url()

    @data("staff", "offering_owner", "offering_manager")
    def test_create_allowed(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        response = self.client.post(
            self.url, _make_payload(self.agent_identity), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        self.assertTrue(
            models.SiteAgentLog.objects.filter(
                agent_identity=self.agent_identity
            ).exists()
        )

    @data("admin", "manager", "owner", "global_support")
    def test_create_forbidden(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        response = self.client.post(
            self.url, _make_payload(self.agent_identity), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.SiteAgentLog.objects.exists())

    def test_create_unauthenticated(self):
        response = self.client.post(
            self.url, _make_payload(self.agent_identity), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_batch_stores_all_entries(self):
        self.client.force_login(self.fixture.staff)
        entries = [
            {
                "agent_identity_uuid": self.agent_identity.uuid.hex,
                "timestamp": 1_746_355_200.0 + i,
                "level": "INFO",
                "message": f"msg {i}",
                "module": "mod",
            }
            for i in range(5)
        ]
        response = self.client.post(self.url, entries, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            models.SiteAgentLog.objects.filter(
                agent_identity=self.agent_identity
            ).count(),
            5,
        )

    def test_create_stores_correct_fields(self):
        self.client.force_login(self.fixture.staff)
        payload = [
            {
                "agent_identity_uuid": self.agent_identity.uuid.hex,
                "timestamp": 1_746_355_999.0,
                "level": "WARNING",
                "message": "disk space low",
                "module": "waldur_site_agent.monitor",
            }
        ]

        self.client.post(self.url, payload, format="json")

        log = models.SiteAgentLog.objects.get(agent_identity=self.agent_identity)
        self.assertEqual(log.agent_identity, self.agent_identity)
        self.assertEqual(log.timestamp, 1_746_355_999.0)
        self.assertEqual(log.level, "WARNING")
        self.assertEqual(log.message, "disk space low")
        self.assertEqual(log.module, "waldur_site_agent.monitor")

    def test_create_unknown_agent_identity_returns_400(self):
        self.client.force_login(self.fixture.staff)
        payload = [
            {
                "agent_identity_uuid": "00000000000000000000000000000000",
                "timestamp": 1_746_355_200.0,
                "level": "INFO",
                "message": "x",
                "module": "m",
            }
        ]

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_invalid_level_returns_400(self):
        self.client.force_login(self.fixture.staff)
        payload = [
            {
                "agent_identity_uuid": self.agent_identity.uuid.hex,
                "timestamp": 1_746_355_200.0,
                "level": "VERBOSE",
                "message": "x",
                "module": "m",
            }
        ]

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_empty_list_returns_201(self):
        self.client.force_login(self.fixture.staff)

        response = self.client.post(self.url, [], format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json(), [])

    def test_create_entry_missing_required_field_returns_400(self):
        self.client.force_login(self.fixture.staff)
        payload = [{"level": "INFO", "message": "x", "module": "m"}]

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class SiteAgentLogListTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = marketplace_enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_identity = factories.AgentIdentityFactory(offering=self.offering)

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

        self.log = factories.SiteAgentLogFactory(
            agent_identity=self.agent_identity, level="ERROR"
        )
        self.url = factories.SiteAgentLogFactory.get_list_url()

    @data("staff", "global_support")
    def test_privileged_users_see_all_logs(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data("offering_owner", "offering_manager", "offering_admin")
    def test_offering_users_see_their_logs(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data("admin", "manager", "owner")
    def test_consumers_see_no_logs(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_unauthenticated_gets_401(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logs_from_other_offering_are_hidden(self):
        other_identity = factories.AgentIdentityFactory()
        factories.SiteAgentLogFactory(agent_identity=other_identity)

        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [r["offering_uuid"] for r in response.json()]
        self.assertTrue(all(u == self.agent_identity.offering.uuid.hex for u in uuids))

    def test_response_contains_expected_fields(self):
        self.client.force_login(self.fixture.staff)

        response = self.client.get(self.url)

        entry = response.json()[0]
        self.assertIn("uuid", entry)
        self.assertIn("offering", entry)
        self.assertIn("offering_uuid", entry)
        self.assertIn("agent_identity_uuid", entry)
        self.assertIn("timestamp", entry)
        self.assertIn("level", entry)
        self.assertIn("message", entry)
        self.assertIn("module", entry)
        self.assertIn("created", entry)

    def test_agent_identity_uuid_in_response(self):
        self.client.force_login(self.fixture.staff)

        response = self.client.get(self.url)

        entry = response.json()[0]
        self.assertEqual(entry["agent_identity_uuid"], self.agent_identity.uuid.hex)


class SiteAgentLogFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = marketplace_enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_identity = factories.AgentIdentityFactory(offering=self.offering)

        self.url = factories.SiteAgentLogFactory.get_list_url()
        self.client.force_login(self.fixture.staff)

        factories.SiteAgentLogFactory(
            agent_identity=self.agent_identity, level="INFO", timestamp=1_746_355_100.0
        )
        factories.SiteAgentLogFactory(
            agent_identity=self.agent_identity, level="ERROR", timestamp=1_746_355_200.0
        )
        factories.SiteAgentLogFactory(
            agent_identity=self.agent_identity,
            level="WARNING",
            timestamp=1_746_355_300.0,
        )

    def test_filter_by_level(self):
        response = self.client.get(self.url, {"level": "ERROR"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["level"], "ERROR")

    def test_filter_by_timestamp_from(self):
        response = self.client.get(self.url, {"timestamp_from": 1_746_355_200.0})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_filter_by_timestamp_to(self):
        response = self.client.get(self.url, {"timestamp_to": 1_746_355_200.0})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_filter_by_offering_uuid(self):
        other_identity = factories.AgentIdentityFactory()
        factories.SiteAgentLogFactory(agent_identity=other_identity)

        response = self.client.get(self.url, {"offering_uuid": self.offering.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 3)

    def test_filter_by_agent_identity_uuid(self):
        other_identity = factories.AgentIdentityFactory(offering=self.offering)
        factories.SiteAgentLogFactory(agent_identity=other_identity)

        response = self.client.get(
            self.url, {"agent_identity_uuid": self.agent_identity.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 3)
