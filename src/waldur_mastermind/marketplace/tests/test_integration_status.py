import datetime

from ddt import data, ddt
from django.utils import timezone
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class IntegrationStatusCreationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.order = self.fixture.order
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        # permissions.yaml is not loaded in tests, so every role starts with no
        # permissions and has to be seeded here. Production ships UPDATE_OFFERING
        # on OFFERING.MANAGER, so seeding it is what makes the offering_manager
        # cases below exercise the real role.
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    def test_integration_status_created_while_orders_fetched(self):
        url = (
            factories.OrderFactory.get_list_url()
            + f"?offering_uuid={self.offering.uuid.hex}"
        )
        self.client.force_login(self.fixture.offering_owner)

        self.assertEqual(
            0,
            models.IntegrationStatus.objects.filter(
                offering=self.offering,
                agent_type=models.IntegrationStatus.AgentTypes.ORDER_PROCESSING,
            ).count(),
        )
        response = self.client.get(
            url, headers={"USER_AGENT": "waldur-site-agent/1.0.0"}
        )
        self.assertEqual(200, response.status_code)

        self.assertEqual(
            1,
            models.IntegrationStatus.objects.filter(
                offering=self.offering,
                agent_type=models.IntegrationStatus.AgentTypes.ORDER_PROCESSING,
            ).count(),
        )

        integration_status = models.IntegrationStatus.objects.get(
            offering=self.offering,
            agent_type=models.IntegrationStatus.AgentTypes.ORDER_PROCESSING,
        )
        self.assertEqual(
            models.IntegrationStatus.States.ACTIVE, integration_status.status
        )
        self.assertIsNotNone(integration_status.last_request_timestamp)

    @data("offering_owner", "service_manager", "offering_manager")
    def test_integration_status_created_while_resources_fetched(self, user):
        url = (
            factories.ResourceFactory.get_list_url()
            + f"?offering_uuid={self.offering.uuid.hex}"
        )
        self.client.force_login(getattr(self.fixture, user))

        self.assertEqual(
            0,
            models.IntegrationStatus.objects.filter(
                offering=self.offering,
                agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING,
            ).count(),
        )
        response = self.client.get(
            url, headers={"USER_AGENT": "waldur-site-agent-report/1.0.0"}
        )
        self.assertEqual(200, response.status_code)

        self.assertEqual(
            1,
            models.IntegrationStatus.objects.filter(
                offering=self.offering,
                agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING,
            ).count(),
        )

        integration_status = models.IntegrationStatus.objects.get(
            offering=self.offering,
            agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING,
        )
        self.assertEqual(
            models.IntegrationStatus.States.ACTIVE, integration_status.status
        )
        self.assertIsNotNone(integration_status.last_request_timestamp)

    @data("offering_admin")
    def test_integration_status_creation_not_permitted_for_consumer_user(self, user):
        url = (
            factories.ResourceFactory.get_list_url()
            + f"?offering_uuid={self.offering.uuid.hex}"
        )
        self.client.force_login(getattr(self.fixture, user))

        self.assertEqual(
            0,
            models.IntegrationStatus.objects.filter(
                offering=self.offering,
                agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING,
            ).count(),
        )
        response = self.client.get(
            url, headers={"USER_AGENT": "waldur-site-agent-report/1.0.0"}
        )
        self.assertEqual(200, response.status_code)

        self.assertEqual(
            0,
            models.IntegrationStatus.objects.filter(
                offering=self.offering,
                agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING,
            ).count(),
        )

    def test_other_site_agent_modes_do_not_refresh_usage_reporting(self):
        """Membership sync and the event agent list the same resources.

        That list keeps resource sync current. It must not move the
        usage-reporting timestamp, or a daily reporter looks freshly reported
        for as long as another mode keeps listing.
        """
        url = (
            factories.ResourceFactory.get_list_url()
            + f"?offering_uuid={self.offering.uuid.hex}"
        )
        self.client.force_login(self.fixture.offering_owner)
        usage_status = factories.IntegrationStatusFactory(
            offering=self.offering,
            agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING,
            status=models.IntegrationStatus.States.ACTIVE,
            service_name="waldur-site-agent-report/0.9.0",
        )
        usage_status.last_request_timestamp = timezone.now() - datetime.timedelta(
            days=3
        )
        usage_status.save(update_fields=["last_request_timestamp", "service_name"])
        reported_at = usage_status.last_request_timestamp

        response = self.client.get(
            url,
            headers={"USER_AGENT": "waldur-site-agent-membership_sync/1.0.0"},
        )
        self.assertEqual(200, response.status_code)

        usage_status.refresh_from_db()
        self.assertEqual(usage_status.last_request_timestamp, reported_at)
        self.assertEqual(usage_status.service_name, "waldur-site-agent-report/0.9.0")
        self.assertTrue(
            models.IntegrationStatus.objects.filter(
                offering=self.offering,
                agent_type=models.IntegrationStatus.AgentTypes.RESOURCE_SYNC,
                status=models.IntegrationStatus.States.ACTIVE,
            ).exists()
        )

        response = self.client.get(
            url, headers={"USER_AGENT": "waldur-site-agent/1.0.0"}
        )
        self.assertEqual(200, response.status_code)
        usage_status.refresh_from_db()
        self.assertEqual(usage_status.last_request_timestamp, reported_at)

    def assert_refreshed(self, *agent_types):
        for agent_type in agent_types:
            integration_status = models.IntegrationStatus.objects.get(
                offering=self.offering, agent_type=agent_type
            )
            self.assertEqual(
                models.IntegrationStatus.States.ACTIVE, integration_status.status
            )
            self.assertIsNotNone(integration_status.last_request_timestamp)
            self.assertEqual(
                integration_status.service_name,
                "waldur-site-agent-event_process/1.0.0",
            )

    def test_order_retrieve_refreshes_status_of_its_offering(self):
        """The event agent fetches the order an event is about.

        The URL names no offering, so the order's own offering is the one the
        request counts for, and nothing is logged about a missing UUID.
        """
        self.client.force_login(self.fixture.offering_owner)

        with self.assertNoLogs("waldur_mastermind.marketplace.utils", "WARNING"):
            response = self.client.get(
                factories.OrderFactory.get_url(self.order),
                headers={"USER_AGENT": "waldur-site-agent-event_process/1.0.0"},
            )

        self.assertEqual(200, response.status_code)
        self.assert_refreshed(
            models.IntegrationStatus.AgentTypes.ORDER_PROCESSING,
            models.IntegrationStatus.AgentTypes.EVENT_PROCESSING,
        )

    def test_provider_resource_retrieve_refreshes_status_of_its_offering(self):
        self.client.force_login(self.fixture.offering_owner)

        with self.assertNoLogs("waldur_mastermind.marketplace.utils", "WARNING"):
            response = self.client.get(
                factories.ResourceFactory.get_provider_resource_url(
                    self.fixture.resource
                ),
                headers={"USER_AGENT": "waldur-site-agent-event_process/1.0.0"},
            )

        self.assertEqual(200, response.status_code)
        self.assert_refreshed(
            models.IntegrationStatus.AgentTypes.RESOURCE_SYNC,
            models.IntegrationStatus.AgentTypes.EVENT_PROCESSING,
        )
        self.assertFalse(
            models.IntegrationStatus.objects.filter(
                agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING
            ).exists()
        )

    def test_agent_token_without_offering_update_refreshes_status(self):
        """The site agent's own token carries OFFERING.MANAGE_AGENT only.

        Reporting what the agent ran is what that permission is for, so it is
        enough to keep the integration status current without OFFERING.UPDATE.
        """
        OfferingRole.MANAGER.delete_permission(PermissionEnum.UPDATE_OFFERING)
        OfferingRole.MANAGER.add_permission(PermissionEnum.MANAGE_OFFERING_AGENT)
        self.client.force_login(self.fixture.offering_manager)

        with self.assertNoLogs("waldur_mastermind.marketplace.utils", "WARNING"):
            response = self.client.get(
                factories.ResourceFactory.get_provider_resource_url(
                    self.fixture.resource
                ),
                headers={"USER_AGENT": "waldur-site-agent-event_process/1.0.0"},
            )

        self.assertEqual(200, response.status_code)
        self.assert_refreshed(
            models.IntegrationStatus.AgentTypes.RESOURCE_SYNC,
            models.IntegrationStatus.AgentTypes.EVENT_PROCESSING,
        )

    def test_retrieve_by_consumer_does_not_refresh_status(self):
        self.client.force_login(self.fixture.owner)

        response = self.client.get(
            factories.ResourceFactory.get_url(self.fixture.resource),
            headers={"USER_AGENT": "waldur-site-agent-event_process/1.0.0"},
        )

        self.assertEqual(200, response.status_code)
        self.assertFalse(models.IntegrationStatus.objects.exists())

    def test_retrieve_without_site_agent_does_not_refresh_status(self):
        self.client.force_login(self.fixture.offering_owner)

        response = self.client.get(
            factories.ResourceFactory.get_provider_resource_url(self.fixture.resource)
        )

        self.assertEqual(200, response.status_code)
        self.assertFalse(models.IntegrationStatus.objects.exists())

    def test_list_without_offering_logs_no_warning(self):
        self.client.force_login(self.fixture.offering_owner)

        with self.assertNoLogs("waldur_mastermind.marketplace.utils", "WARNING"):
            response = self.client.get(
                factories.ResourceFactory.get_provider_resource_list_url(),
                headers={"USER_AGENT": "waldur-site-agent-event_process/1.0.0"},
            )

        self.assertEqual(200, response.status_code)
        self.assertFalse(models.IntegrationStatus.objects.exists())


@ddt
class IntegrationStatusGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.waldur_site_agent_name = "waldur-site-agent/1.0.0"
        factories.IntegrationStatusFactory(
            offering=self.offering,
            agent_type=models.IntegrationStatus.AgentTypes.ORDER_PROCESSING,
            service_name=self.waldur_site_agent_name,
            status=models.IntegrationStatus.States.ACTIVE,
        )
        factories.IntegrationStatusFactory(
            offering=self.offering,
            agent_type=models.IntegrationStatus.AgentTypes.USAGE_REPORTING,
            service_name=self.waldur_site_agent_name,
            status=models.IntegrationStatus.States.ACTIVE,
        )
        factories.IntegrationStatusFactory(
            offering=self.offering,
            agent_type=models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
            status=models.IntegrationStatus.States.ACTIVE,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        # See the note in IntegrationStatusCreationTest.setUp: roles carry no
        # permissions in tests until seeded here.
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    @data("offering_owner", "service_manager", "offering_manager")
    def test_service_provider_user_can_see_integration_statuses_in_offering(self, user):
        self.client.force_login(getattr(self.fixture, user))
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(200, response.status_code)

        self.assertEqual(3, len(response.data["integration_status"]), response.data)

        integration_status_data = response.data["integration_status"][0]

        self.assertEqual(integration_status_data["agent_type"], "Order processing")
        self.assertEqual(
            integration_status_data["service_name"], self.waldur_site_agent_name
        )
        self.assertEqual(integration_status_data["status"], "Active")

    @data("offering_admin")
    def test_consumer_user_can_not_see_integration_statuses_in_offering(self, user):
        """A project admin administers resources, not the offering, and holds no
        UPDATE_OFFERING on any scope."""
        self.client.force_login(getattr(self.fixture, user))
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(200, response.status_code)

        self.assertIsNone(response.data["integration_status"], response.data)

    @data("offering_owner", "service_manager", "offering_manager")
    def test_service_provider_user_can_view_integration_statuses(self, user):
        self.client.force_login(getattr(self.fixture, user))
        url = factories.IntegrationStatusFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(200, response.status_code)

        self.assertEqual(3, len(response.data), response.data)

    @data("offering_admin")
    def test_consumer_user_can_not_view_integration_statuses(self, user):
        self.client.force_login(getattr(self.fixture, user))
        url = factories.IntegrationStatusFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(200, response.status_code)

        self.assertEqual(0, len(response.data), response.data)
