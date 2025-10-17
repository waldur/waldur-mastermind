from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent.tests import factories


@ddt
class AgentServiceSetStatisticsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent"
        )
        self.agent_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
            name="event_processor",
            mode="event_processing",
        )

    def _get_set_statistics_url(self):
        """Helper method to get the set statistics URL."""
        return factories.AgentServiceFactory.get_url(
            self.agent_service, action="set_statistics"
        )

    @data("staff", "offering_owner")
    def test_set_statistics_success(self, user_role):
        """Test successful update of service statistics."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_set_statistics_url()

        payload = {
            "statistics": {
                "total_processed": 100,
                "failed_count": 5,
                "last_processed_at": "2024-01-01T12:00:00Z",
            }
        }

        # Verify initial statistics is empty
        self.assertEqual(self.agent_service.statistics, {})

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())

        # Verify statistics was updated
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.statistics, payload["statistics"])

        # Verify response contains updated data
        response_data = response.json()
        self.assertEqual(response_data["statistics"], payload["statistics"])

    @data("staff", "offering_owner")
    def test_set_statistics_overwrites_existing(self, user_role):
        """Test that set_statistics overwrites existing statistics."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_set_statistics_url()

        # Set initial statistics
        initial_stats = {"initial_key": "initial_value"}
        self.agent_service.statistics = initial_stats
        self.agent_service.save()

        # Update with new statistics
        new_stats = {
            "new_key": "new_value",
            "count": 42,
        }
        payload = {"statistics": new_stats}

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify old statistics is completely replaced
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.statistics, new_stats)
        self.assertNotIn("initial_key", self.agent_service.statistics)

    @data("staff", "offering_owner")
    def test_set_statistics_with_complex_data(self, user_role):
        """Test set_statistics with complex nested JSON data."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_set_statistics_url()

        payload = {
            "statistics": {
                "metrics": {
                    "cpu": {"average": 45.5, "peak": 89.3},
                    "memory": {"used": 4096, "total": 8192},
                },
                "counts": [10, 20, 30, 40],
                "enabled": True,
            }
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify complex data structure is preserved
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.statistics, payload["statistics"])

    @data("offering_manager", "offering_admin", "admin", "manager", "global_support")
    def test_set_statistics_forbidden(self, user_role):
        """Test that forbidden roles cannot set statistics."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_set_statistics_url()

        payload = {"statistics": {"test": "data"}}

        response = self.client.post(url, payload, format="json")
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
            response.json(),
        )

        # Verify statistics was not updated
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.statistics, {})

    def test_set_statistics_missing_statistics_field(self):
        """Test validation error when statistics field is missing."""
        user = self.fixture.staff
        self.client.force_login(user)

        url = self._get_set_statistics_url()

        # Missing "statistics" field in payload
        payload = {"other_field": "value"}

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify error message
        response_data = response.json()
        self.assertIn("statistics", response_data)

    def test_set_statistics_invalid_json(self):
        """Test validation error for invalid JSON data."""
        user = self.fixture.staff
        self.client.force_login(user)

        url = self._get_set_statistics_url()

        # Send invalid content type (not JSON)
        response = self.client.post(url, "invalid json", content_type="text/plain")
        self.assertEqual(
            response.status_code,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            response.json(),
        )

    def test_set_statistics_empty_statistics(self):
        """Test that empty statistics object is allowed."""
        user = self.fixture.staff
        self.client.force_login(user)

        url = self._get_set_statistics_url()

        payload = {"statistics": {}}

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify empty statistics was set
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.statistics, {})

    def test_set_statistics_nonexistent_service(self):
        """Test 404 error for non-existent service."""
        user = self.fixture.staff
        self.client.force_login(user)

        # Use a non-existent UUID
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        url = f"http://testserver/api/marketplace-site-agent-services/{fake_uuid}/set_statistics/"

        payload = {"statistics": {"test": "data"}}

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_set_statistics_unauthenticated(self):
        """Test that unauthenticated users cannot set statistics."""
        url = self._get_set_statistics_url()

        payload = {"statistics": {"test": "data"}}

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # Verify statistics was not updated
        self.agent_service.refresh_from_db()
        self.assertEqual(self.agent_service.statistics, {})


@ddt
class AgentServiceListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.offering_admin
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_service = factories.AgentServiceFactory(name="Test Agent Service")
        identity = self.agent_service.identity
        identity.offering = self.offering
        identity.save()

    @data("staff", "offering_owner", "offering_manager", "offering_admin")
    def test_agent_service_list(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = factories.AgentServiceFactory.get_list_url()
        agent_processor = factories.AgentProcessorFactory(service=self.agent_service)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())

        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], self.agent_service.name)
        self.assertEqual(len(results[0]["processors"]), 1)
        self.assertEqual(results[0]["processors"][0]["uuid"], agent_processor.uuid.hex)

    @data("admin", "manager", "owner")
    def test_consumers_can_not_see_agent_services(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = factories.AgentServiceFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


@ddt
class AgentServiceRegisterProcessorTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent"
        )
        self.agent_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
            name="event_processor",
            mode="event_processing",
        )

    def _get_register_processor_url(self):
        """Helper method to get the register processor URL."""
        return factories.AgentServiceFactory.get_url(
            self.agent_service, action="register_processor"
        )

    @data("staff", "offering_owner")
    def test_register_processor_success(self, user_role):
        """Test successful registration of a new processor."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_processor_url()

        payload = {
            "name": "order_processor",
            "backend_type": "SLURM",
            "backend_version": "23.02.1",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

        # Verify processor was created
        self.assertEqual(self.agent_service.agentprocessor_set.count(), 1)
        processor = self.agent_service.agentprocessor_set.first()
        self.assertEqual(processor.name, payload["name"])
        self.assertEqual(processor.backend_type, payload["backend_type"])
        self.assertEqual(processor.backend_version, payload["backend_version"])

        # Verify response contains processor data
        response_data = response.json()
        self.assertEqual(response_data["name"], payload["name"])
        self.assertEqual(response_data["backend_type"], payload["backend_type"])
        self.assertEqual(response_data["backend_version"], payload["backend_version"])
        self.assertEqual(response_data["service_name"], self.agent_service.name)
        self.assertIn("uuid", response_data)
        self.assertIn("url", response_data)

    @data("staff", "offering_owner")
    def test_register_processor_without_backend_version(self, user_role):
        """Test registering processor without optional backend_version."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_processor_url()

        payload = {
            "name": "membership_sync",
            "backend_type": "LDAP",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify processor was created without backend_version
        processor = self.agent_service.agentprocessor_set.first()
        self.assertEqual(processor.name, payload["name"])
        self.assertEqual(processor.backend_type, payload["backend_type"])
        self.assertIsNone(processor.backend_version)

    @data("staff", "offering_owner")
    def test_register_multiple_processors(self, user_role):
        """Test registering multiple processors for the same service."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_processor_url()

        # Register first processor
        payload1 = {
            "name": "processor_1",
            "backend_type": "TYPE_A",
        }
        response1 = self.client.post(url, payload1, format="json")
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Register second processor
        payload2 = {
            "name": "processor_2",
            "backend_type": "TYPE_B",
        }
        response2 = self.client.post(url, payload2, format="json")
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)

        # Verify both processors exist
        self.assertEqual(self.agent_service.agentprocessor_set.count(), 2)
        processor_names = list(
            self.agent_service.agentprocessor_set.values_list("name", flat=True)
        )
        self.assertIn(payload1["name"], processor_names)
        self.assertIn(payload2["name"], processor_names)

    @data("offering_manager", "offering_admin", "admin", "manager", "global_support")
    def test_register_processor_forbidden(self, user_role):
        """Test that forbidden roles cannot register processors."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_processor_url()

        payload = {
            "name": "test_processor",
            "backend_type": "TEST",
        }

        response = self.client.post(url, payload, format="json")
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
            response.json(),
        )

        # Verify processor was not created
        self.assertEqual(self.agent_service.agentprocessor_set.count(), 0)

    def test_register_processor_missing_required_field(self):
        """Test validation error when required fields are missing."""
        user = self.fixture.staff
        self.client.force_login(user)

        url = self._get_register_processor_url()

        # Missing "name" field
        payload = {
            "backend_type": "SLURM",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify error message
        response_data = response.json()
        self.assertIn("name", response_data)

        # Verify processor was not created
        self.assertEqual(self.agent_service.agentprocessor_set.count(), 0)

    def test_register_processor_missing_backend_type(self):
        """Test validation error when backend_type is missing."""
        user = self.fixture.staff
        self.client.force_login(user)

        url = self._get_register_processor_url()

        payload = {
            "name": "test_processor",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify error message
        response_data = response.json()
        self.assertIn("backend_type", response_data)

        # Verify processor was not created
        self.assertEqual(self.agent_service.agentprocessor_set.count(), 0)

    def test_register_processor_duplicate_name(self):
        """Test validation error when registering processor with duplicate name for same service."""
        user = self.fixture.staff
        self.client.force_login(user)

        url = self._get_register_processor_url()

        payload = {
            "name": "unique_processor",
            "backend_type": "SLURM",
            "backend_version": "23.02.0",
        }

        # First registration should succeed
        response1 = self.client.post(url, payload, format="json")
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Second registration with same name should fail due to unique_together constraint
        payload["backend_version"] = "23.02.1"  # Change version to differentiate

        response2 = self.client.post(url, payload, format="json")
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        self.assertEqual(response1.json()["uuid"], response2.json()["uuid"])
        self.assertEqual(
            response2.json()["backend_version"], payload["backend_version"]
        )

        # Verify only one processor exists
        self.assertEqual(self.agent_service.agentprocessor_set.count(), 1)
        self.assertEqual(
            self.agent_service.agentprocessor_set.first().backend_version,
            payload["backend_version"],
        )

    def test_register_processor_unauthenticated(self):
        """Test that unauthenticated users cannot register processors."""
        url = self._get_register_processor_url()

        payload = {
            "name": "test_processor",
            "backend_type": "SLURM",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # Verify processor was not created
        self.assertEqual(self.agent_service.agentprocessor_set.count(), 0)

    def test_register_processor_nonexistent_service(self):
        """Test 404 error for non-existent service."""
        user = self.fixture.staff
        self.client.force_login(user)

        # Use a non-existent UUID
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        url = f"http://testserver/api/marketplace-site-agent-services/{fake_uuid}/register_processor/"

        payload = {
            "name": "test_processor",
            "backend_type": "SLURM",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class AgentProcessorListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.offering_admin
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_processor = factories.AgentProcessorFactory(
            name="Test Agent Processor"
        )
        identity = self.agent_processor.service.identity
        identity.offering = self.offering
        identity.save()

    @data("staff", "offering_owner", "offering_manager", "offering_admin")
    def test_agent_processor_list(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = factories.AgentProcessorFactory.get_list_url()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())

        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], self.agent_processor.name)

    @data("admin", "manager", "owner")
    def test_consumers_can_not_see_agent_processors(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = factories.AgentProcessorFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)
