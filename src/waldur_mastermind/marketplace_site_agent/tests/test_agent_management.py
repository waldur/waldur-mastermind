from datetime import timedelta
from unittest import mock

from ddt import data, ddt
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent import models
from waldur_mastermind.marketplace_site_agent.enums import AgentServiceState
from waldur_mastermind.marketplace_site_agent.tests import factories


@ddt
class AgentServiceDestroyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent Identity"
        )
        self.agent_service = factories.AgentServiceFactory(
            identity=self.agent_identity, name="Test Service"
        )

    @data("staff", "offering_owner")
    def test_destroy_allowed(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = factories.AgentServiceFactory.get_url(self.agent_service)

        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.AgentService.objects.filter(uuid=self.agent_service.uuid).exists()
        )

    @data("offering_manager", "offering_admin", "admin", "manager")
    def test_destroy_forbidden(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = factories.AgentServiceFactory.get_url(self.agent_service)

        response = self.client.delete(url)
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )
        self.assertTrue(
            models.AgentService.objects.filter(uuid=self.agent_service.uuid).exists()
        )


@ddt
class AgentProcessorDestroyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent Identity"
        )
        self.agent_service = factories.AgentServiceFactory(
            identity=self.agent_identity, name="Test Service"
        )
        self.agent_processor = factories.AgentProcessorFactory(
            service=self.agent_service, name="Test Processor"
        )

    def _get_processor_url(self):
        return "http://testserver" + reverse(
            "marketplace-site-agent-processor-detail",
            kwargs={"uuid": self.agent_processor.uuid.hex},
        )

    @data("staff", "offering_owner")
    def test_destroy_allowed(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_processor_url()

        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.AgentProcessor.objects.filter(
                uuid=self.agent_processor.uuid
            ).exists()
        )

    @data("offering_manager", "offering_admin", "admin", "manager")
    def test_destroy_forbidden(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_processor_url()

        response = self.client.delete(url)
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )
        self.assertTrue(
            models.AgentProcessor.objects.filter(
                uuid=self.agent_processor.uuid
            ).exists()
        )


@ddt
class AgentIdentityCleanupOrphanedTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        # Create orphaned identity (no services)
        self.orphaned_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Orphaned Identity"
        )

        # Create identity with service
        self.active_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Active Identity"
        )
        self.agent_service = factories.AgentServiceFactory(
            identity=self.active_identity, name="Active Service"
        )

    def _get_cleanup_url(self):
        return factories.AgentIdentityFactory.get_list_url(action="cleanup_orphaned")

    def test_cleanup_dry_run(self):
        self.client.force_login(self.fixture.staff)
        url = self._get_cleanup_url()

        response = self.client.post(url, {"dry_run": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(data["deleted_count"], 1)
        self.assertTrue(data["dry_run"])
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["name"], "Orphaned Identity")

        # Verify nothing was actually deleted
        self.assertTrue(
            models.AgentIdentity.objects.filter(
                uuid=self.orphaned_identity.uuid
            ).exists()
        )

    def test_cleanup_execute(self):
        self.client.force_login(self.fixture.staff)
        url = self._get_cleanup_url()

        response = self.client.post(url, {"dry_run": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(data["deleted_count"], 1)
        self.assertFalse(data["dry_run"])

        # Verify orphaned identity was deleted
        self.assertFalse(
            models.AgentIdentity.objects.filter(
                uuid=self.orphaned_identity.uuid
            ).exists()
        )
        # Verify active identity still exists
        self.assertTrue(
            models.AgentIdentity.objects.filter(uuid=self.active_identity.uuid).exists()
        )

    @data("offering_owner", "offering_manager", "offering_admin", "admin", "manager")
    def test_cleanup_forbidden_for_non_staff(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_cleanup_url()

        response = self.client.post(url, {"dry_run": True})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AgentServiceCleanupStaleTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Identity"
        )

        # Create stale service (modified 48 hours ago)
        self.stale_service = factories.AgentServiceFactory(
            identity=self.agent_identity, name="Stale Service"
        )
        models.AgentService.objects.filter(uuid=self.stale_service.uuid).update(
            modified=timezone.now() - timedelta(hours=48)
        )

        # Create fresh service
        self.fresh_service = factories.AgentServiceFactory(
            identity=self.agent_identity, name="Fresh Service"
        )

    def _get_cleanup_url(self):
        return factories.AgentServiceFactory.get_list_url(action="cleanup_stale")

    def test_cleanup_dry_run(self):
        self.client.force_login(self.fixture.staff)
        url = self._get_cleanup_url()

        response = self.client.post(url, {"dry_run": True, "older_than_hours": 24})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(data["deleted_count"], 1)
        self.assertTrue(data["dry_run"])
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["name"], "Stale Service")

        # Verify nothing was actually deleted
        self.assertTrue(
            models.AgentService.objects.filter(uuid=self.stale_service.uuid).exists()
        )

    def test_cleanup_execute(self):
        self.client.force_login(self.fixture.staff)
        url = self._get_cleanup_url()

        response = self.client.post(url, {"dry_run": False, "older_than_hours": 24})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(data["deleted_count"], 1)
        self.assertFalse(data["dry_run"])

        # Verify stale service was deleted
        self.assertFalse(
            models.AgentService.objects.filter(uuid=self.stale_service.uuid).exists()
        )
        # Verify fresh service still exists
        self.assertTrue(
            models.AgentService.objects.filter(uuid=self.fresh_service.uuid).exists()
        )

    @data("offering_owner", "offering_manager", "offering_admin", "admin", "manager")
    def test_cleanup_forbidden_for_non_staff(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_cleanup_url()

        response = self.client.post(url, {"dry_run": True})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AgentStatsViewSetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Identity"
        )
        self.agent_service = factories.AgentServiceFactory(
            identity=self.agent_identity, name="Test Service"
        )
        self.agent_processor = factories.AgentProcessorFactory(
            service=self.agent_service, name="Test Processor"
        )

    def _get_stats_url(self):
        return "/api/marketplace-site-agent-stats/"

    @data("staff", "global_support")
    def test_stats_access_allowed(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_stats_url()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("identities", data)
        self.assertIn("services", data)
        self.assertIn("processors", data)

        self.assertEqual(data["identities"]["total"], 1)
        self.assertEqual(data["services"]["total"], 1)
        self.assertEqual(data["processors"]["total"], 1)

    @data("offering_owner", "offering_manager", "admin", "manager")
    def test_stats_forbidden_for_non_support(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_stats_url()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_stats_by_state(self):
        # Create services with different states
        factories.AgentServiceFactory(
            identity=self.agent_identity,
            name="Idle Service",
            state=AgentServiceState.IDLE,
        )
        factories.AgentServiceFactory(
            identity=self.agent_identity,
            name="Error Service",
            state=AgentServiceState.ERROR,
        )

        self.client.force_login(self.fixture.staff)
        url = self._get_stats_url()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(data["services"]["total"], 3)
        self.assertEqual(data["services"]["by_state"]["active"], 1)
        self.assertEqual(data["services"]["by_state"]["idle"], 1)
        self.assertEqual(data["services"]["by_state"]["error"], 1)


@ddt
class AgentTaskStatsViewSetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()

    def _get_task_stats_url(self):
        return "/api/marketplace-site-agent-task-stats/"

    @data("staff", "global_support")
    @mock.patch("waldur_core.server.celeryconf.app")
    def test_task_stats_access_allowed(self, user_role, mock_app):
        mock_inspect = mock.MagicMock()
        mock_inspect.active.return_value = {}
        mock_inspect.scheduled.return_value = {}
        mock_inspect.reserved.return_value = {}
        mock_app.control.inspect.return_value = mock_inspect

        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_task_stats_url()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("active_tasks", data)
        self.assertIn("scheduled_tasks", data)
        self.assertIn("reserved_tasks", data)

    @data("offering_owner", "offering_manager", "admin", "manager")
    def test_task_stats_forbidden_for_non_support(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = self._get_task_stats_url()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AgentServiceStaleFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Identity"
        )

        # Create stale service
        self.stale_service = factories.AgentServiceFactory(
            identity=self.agent_identity, name="Stale Service"
        )
        models.AgentService.objects.filter(uuid=self.stale_service.uuid).update(
            modified=timezone.now() - timedelta(hours=48)
        )

        # Create fresh service
        self.fresh_service = factories.AgentServiceFactory(
            identity=self.agent_identity, name="Fresh Service"
        )

    def test_filter_stale_true(self):
        self.client.force_login(self.fixture.staff)
        url = factories.AgentServiceFactory.get_list_url()

        response = self.client.get(url, {"stale": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Stale Service")

    def test_filter_stale_false(self):
        self.client.force_login(self.fixture.staff)
        url = factories.AgentServiceFactory.get_list_url()

        response = self.client.get(url, {"stale": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Fresh Service")


@ddt
class AgentIdentityOrphanedFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        # Create orphaned identity (no services)
        self.orphaned_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Orphaned Identity"
        )

        # Create identity with service
        self.active_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Active Identity"
        )
        factories.AgentServiceFactory(
            identity=self.active_identity, name="Active Service"
        )

    def test_filter_orphaned_true(self):
        self.client.force_login(self.fixture.staff)
        url = factories.AgentIdentityFactory.get_list_url()

        response = self.client.get(url, {"orphaned": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Orphaned Identity")

    def test_filter_orphaned_false(self):
        self.client.force_login(self.fixture.staff)
        url = factories.AgentIdentityFactory.get_list_url()

        response = self.client.get(url, {"orphaned": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Active Identity")
