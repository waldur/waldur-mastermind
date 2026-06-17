import textwrap
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.logging import enums as logging_enums
from waldur_core.logging import models as logging_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent import models
from waldur_mastermind.marketplace_site_agent.tests import factories


@ddt
class AgentIdentityCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.payload = {
            "name": "Agent Test 00",
            "offering": self.offering.uuid.hex,
            "config_file_path": "/etc/waldur/test-agent.yaml",
            "config_file_content": textwrap.dedent(f"""
                offerings:
                - name: {self.offering.name}
                  waldur_api_url: "http://testserver/"
                  waldur_offering_uuid: "{self.offering.uuid.hex}"
                  stomp_enabled: true
                  websocket_use_tls: false
                  backend_type: "slurm"
                  order_processing_backend: "slurm"
                  membership_sync_backend: "slurm"
                  reporting_backend: "slurm"
                  username_management_backend: "base"
                  resource_import_enabled: false
            """),
            "dependencies": [
                {"package": "pyyaml", "version": "v6.0.1"},
                {"package": "requests", "version": "v2.32.3"},
                {"package": "sentry-sdk", "version": "v2.3.1"},
                {"package": "stomp-py", "version": "v8.2.0"},
                {"package": "types-pyyaml", "version": "v6.0.12.20250822"},
                {"package": "waldur-api-client", "version": "v7.8.0"},
                {"package": "ruff", "version": "v0.12.11"},
            ],
        }

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    @data("staff", "offering_owner", "offering_manager")
    def test_agent_identity_create_allowed(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = factories.AgentIdentityFactory.get_list_url()

        response = self.client.post(url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

        agent_identity = models.AgentIdentity.objects.get(
            name="Agent Test 00", offering=self.offering
        )
        self.assertEqual(agent_identity.dependencies, self.payload["dependencies"])
        self.assertEqual(response.json()["dependencies"], self.payload["dependencies"])

    @data("offering_admin", "admin", "manager", "global_support")
    def test_agent_identity_create_forbidden(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = factories.AgentIdentityFactory.get_list_url()

        response = self.client.post(url, self.payload)
        self.assertEqual(
            response.status_code, status.HTTP_403_FORBIDDEN, response.json()
        )


@ddt
class AgentIdentityListTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.offering_admin
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent Identity"
        )

    @data("staff", "offering_owner", "offering_manager", "offering_admin")
    def test_agent_identity_list(self, user_role):
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        url = factories.AgentIdentityFactory.get_list_url()
        agent_service = factories.AgentServiceFactory(
            identity=self.agent_identity,
        )

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())

        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], self.agent_identity.name)
        self.assertEqual(len(results[0]["services"]), 1)
        self.assertEqual(results[0]["services"][0]["uuid"], agent_service.uuid.hex)

    @data("admin", "manager", "owner")
    def test_consumers_can_not_see_agent_identities(self, user_role):
        """Test that consumers cannot see agent identities."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = factories.AgentIdentityFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


@ddt
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
class AgentIdentityEventSubscriptionTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    def _create_agent_identity(self):
        """Helper method to create an AgentIdentity instance for testing."""
        return factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent Identity"
        )

    def _get_register_event_subscription_url(self, agent_identity):
        """Helper method to get the register event subscription URL."""
        return factories.AgentIdentityFactory.get_url(
            agent_identity, action="register_event_subscription"
        )

    @data("staff", "offering_owner", "offering_manager")
    def test_register_event_subscription_success(
        self,
        user_role,
        mock_create_rabbitmq_virtual_host,
        mock_create_rabbitmq_user,
        mock_assign_rabbitmq_vhost_permissions,
    ):
        """Test successful creation of event subscription."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_event_subscription_url(agent_identity)

        payload = {
            "observable_object_type": logging_enums.ObservableObjectType.ORDER.value,
            "description": "Test event subscription for orders",
        }

        # Ensure no existing subscriptions
        self.assertEqual(logging_models.EventSubscription.objects.count(), 0)

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

        # Verify subscription was created
        self.assertEqual(logging_models.EventSubscription.objects.count(), 1)
        subscription = logging_models.EventSubscription.objects.first()
        self.assertEqual(subscription.user, user)
        self.assertEqual(subscription.description, payload["description"])
        self.assertEqual(
            subscription.observable_objects,
            [{"object_type": payload["observable_object_type"]}],
        )

        # Verify response data
        response_data = response.json()
        self.assertEqual(response_data["description"], payload["description"])
        self.assertTrue(user.uuid.hex in response_data["user"])

        mock_create_rabbitmq_virtual_host.assert_called_once()
        mock_create_rabbitmq_user.assert_called_once()
        mock_assign_rabbitmq_vhost_permissions.assert_called_once()

    @data("staff", "offering_owner")
    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_vhost_permissions"
    )
    def test_register_event_subscription_existing_returns_200(
        self,
        user_role,
        mock_list_rabbitmq_vhost_permissions,
        mock_get_user,
        mock_assign_rabbitmq_vhost_permissions,
        mock_create_rabbitmq_user,
        mock_create_rabbitmq_virtual_host,
    ):
        """Test that registering same subscription returns existing one with 200 status when RabbitMQ is ready."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_event_subscription_url(agent_identity)

        payload = {
            "observable_object_type": logging_enums.ObservableObjectType.RESOURCE.value,
        }

        # Create first subscription
        response1 = self.client.post(url, payload)
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(logging_models.EventSubscription.objects.count(), 1)

        subscription_uuid = response1.json()["uuid"]

        # Mock RabbitMQ validation to return True (user exists and has permissions)
        mock_get_user.return_value = {"name": subscription_uuid}
        mock_list_rabbitmq_vhost_permissions.return_value = [subscription_uuid]

        # Attempt to create same subscription again
        response2 = self.client.post(url, payload)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # Verify no duplicate was created
        self.assertEqual(logging_models.EventSubscription.objects.count(), 1)

        # Verify same subscription data is returned
        self.assertEqual(response1.json()["uuid"], response2.json()["uuid"])

        # RabbitMQ setup methods should only be called once (during first creation)
        mock_create_rabbitmq_virtual_host.assert_called_once()
        mock_create_rabbitmq_user.assert_called_once()
        mock_assign_rabbitmq_vhost_permissions.assert_called_once()

        # Validation methods should be called on second attempt
        mock_get_user.assert_called_once()
        mock_list_rabbitmq_vhost_permissions.assert_called_once()

    @data("staff", "offering_owner")
    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user"
    )
    def test_register_event_subscription_recreates_when_not_ready(
        self,
        user_role,
        mock_delete_rabbitmq_user,
        mock_get_user,
        mock_assign_rabbitmq_vhost_permissions,
        mock_create_rabbitmq_user,
        mock_create_rabbitmq_virtual_host,
    ):
        """Test that subscription is recreated when RabbitMQ validation fails."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_event_subscription_url(agent_identity)

        payload = {
            "observable_object_type": logging_enums.ObservableObjectType.RESOURCE.value,
        }

        # Create first subscription
        response1 = self.client.post(url, payload)
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(logging_models.EventSubscription.objects.count(), 1)

        first_subscription_uuid = response1.json()["uuid"]

        # Mock RabbitMQ validation to return False (user doesn't exist or no permissions)
        mock_get_user.return_value = None  # User doesn't exist

        # Attempt to create same subscription again - should delete old and create new
        response2 = self.client.post(url, payload)
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)

        # Verify old subscription was deleted and new one created
        self.assertEqual(logging_models.EventSubscription.objects.count(), 1)
        second_subscription_uuid = response2.json()["uuid"]
        self.assertNotEqual(first_subscription_uuid, second_subscription_uuid)

        # Verify old subscription was deleted from RabbitMQ
        mock_delete_rabbitmq_user.assert_called_once_with(first_subscription_uuid)

        # RabbitMQ setup methods should be called twice (once for each creation)
        self.assertEqual(mock_create_rabbitmq_virtual_host.call_count, 2)
        self.assertEqual(mock_create_rabbitmq_user.call_count, 2)
        self.assertEqual(mock_assign_rabbitmq_vhost_permissions.call_count, 2)

        # Validation methods should be called on second attempt
        mock_get_user.assert_called_once()

    def test_register_event_subscription_default_description(
        self,
        mock_create_rabbitmq_virtual_host,
        mock_create_rabbitmq_user,
        mock_assign_rabbitmq_vhost_permissions,
    ):
        """Test that default description is generated when not provided."""
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_event_subscription_url(agent_identity)

        payload = {
            "observable_object_type": logging_enums.ObservableObjectType.USER_ROLE.value,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

        # Verify default description was generated
        expected_description = f"Event subscription for {payload['observable_object_type']} ({agent_identity.name})"
        subscription = logging_models.EventSubscription.objects.first()
        self.assertEqual(subscription.description, expected_description)

        mock_create_rabbitmq_virtual_host.assert_called_once()
        mock_create_rabbitmq_user.assert_called_once()
        mock_assign_rabbitmq_vhost_permissions.assert_called_once()

    @data("offering_admin", "admin", "manager", "global_support")
    def test_register_event_subscription_forbidden(
        self,
        user_role,
        mock_create_rabbitmq_virtual_host,
        mock_create_rabbitmq_user,
        mock_assign_rabbitmq_vhost_permissions,
    ):
        """Test that forbidden roles cannot register event subscriptions."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_event_subscription_url(agent_identity)

        payload = {
            "observable_object_type": logging_enums.ObservableObjectType.ORDER.value,
        }

        response = self.client.post(url, payload)
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
            response.json(),
        )

        # Verify no subscription was created
        self.assertEqual(logging_models.EventSubscription.objects.count(), 0)

        self.assertFalse(mock_create_rabbitmq_virtual_host.called)
        self.assertFalse(mock_create_rabbitmq_user.called)
        self.assertFalse(mock_assign_rabbitmq_vhost_permissions.called)

    def test_register_event_subscription_invalid_object_type(
        self,
        mock_create_rabbitmq_virtual_host,
        mock_create_rabbitmq_user,
        mock_assign_rabbitmq_vhost_permissions,
    ):
        """Test validation error for invalid observable_object_type."""
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_event_subscription_url(agent_identity)

        payload = {
            "observable_object_type": "INVALID_TYPE",
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify error message
        response_data = response.json()
        self.assertIn("observable_object_type", response_data)

        self.assertFalse(mock_create_rabbitmq_virtual_host.called)
        self.assertFalse(mock_create_rabbitmq_user.called)
        self.assertFalse(mock_assign_rabbitmq_vhost_permissions.called)

    def test_register_event_subscription_missing_object_type(
        self,
        mock_create_rabbitmq_virtual_host,
        mock_create_rabbitmq_user,
        mock_assign_rabbitmq_vhost_permissions,
    ):
        """Test validation error when observable_object_type is missing."""
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_event_subscription_url(agent_identity)

        payload = {
            "description": "Test subscription without object type",
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify error message
        response_data = response.json()
        self.assertIn("observable_object_type", response_data)

        self.assertFalse(mock_create_rabbitmq_virtual_host.called)
        self.assertFalse(mock_create_rabbitmq_user.called)
        self.assertFalse(mock_assign_rabbitmq_vhost_permissions.called)

    def test_register_event_subscription_nonexistent_identity(
        self,
        mock_create_rabbitmq_virtual_host,
        mock_create_rabbitmq_user,
        mock_assign_rabbitmq_vhost_permissions,
    ):
        """Test 404 error for non-existent agent identity."""
        user = self.fixture.staff
        self.client.force_login(user)

        # Use a non-existent UUID
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        url = f"/api/marketplace-site-agent-identities/{fake_uuid}/register_event_subscription/"

        payload = {
            "observable_object_type": logging_enums.ObservableObjectType.ORDER.value,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.assertFalse(mock_create_rabbitmq_virtual_host.called)
        self.assertFalse(mock_create_rabbitmq_user.called)
        self.assertFalse(mock_assign_rabbitmq_vhost_permissions.called)


@ddt
class AgentIdentityRegisterServiceTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

        self.agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent Identity"
        )

    def _get_register_service_url(self):
        """Helper method to get the register service URL."""
        return factories.AgentIdentityFactory.get_url(
            self.agent_identity, action="register_service"
        )

    @data("staff", "offering_owner", "offering_manager")
    def test_register_service_success(self, user_role):
        """Test successful registration of a new service."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_service_url()

        payload = {
            "name": "event_processor",
            "mode": "event_processing",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

        # Verify service was created
        self.assertTrue(
            models.AgentService.objects.filter(
                identity=self.agent_identity,
                name=payload["name"],
            ).exists()
        )

        service = models.AgentService.objects.get(
            identity=self.agent_identity,
            name=payload["name"],
        )
        self.assertEqual(service.mode, payload["mode"])

        # Verify response contains service data
        response_data = response.json()
        self.assertEqual(response_data["name"], payload["name"])
        self.assertEqual(response_data["mode"], payload["mode"])
        self.assertEqual(response_data["identity_name"], self.agent_identity.name)
        self.assertIn("uuid", response_data)
        self.assertIn("url", response_data)

    @data("staff", "offering_owner")
    def test_register_service_without_mode(self, user_role):
        """Test registering service without optional mode."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_service_url()

        payload = {
            "name": "usage_reporter",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify service was created without mode
        service = models.AgentService.objects.get(
            identity=self.agent_identity,
            name=payload["name"],
        )
        self.assertEqual(service.name, payload["name"])
        self.assertIsNone(service.mode)

    @data("staff", "offering_owner")
    def test_register_service_existing_returns_200(self, user_role):
        """Test that registering same service returns existing one with 200 status."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_service_url()

        payload = {
            "name": "event_processor",
            "mode": "event_processing",
        }

        # Create first service
        response1 = self.client.post(url, payload, format="json")
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.AgentService.objects.count(), 1)

        # Attempt to create same service again
        payload["mode"] = "event_processing_new"

        response2 = self.client.post(url, payload, format="json")
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # Verify no duplicate was created
        self.assertEqual(models.AgentService.objects.count(), 1)
        self.assertEqual(models.AgentService.objects.first().mode, payload["mode"])

        # Verify same service data is returned
        self.assertEqual(response1.json()["uuid"], response2.json()["uuid"])
        self.assertEqual(response2.json()["mode"], payload["mode"])

    @data("staff", "offering_owner", "offering_manager")
    def test_register_multiple_services(self, user_role):
        """Test registering multiple services for the same identity."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_service_url()

        # Register first service
        payload1 = {
            "name": "event_processor",
            "mode": "event_processing",
        }
        response1 = self.client.post(url, payload1, format="json")
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Register second service
        payload2 = {
            "name": "usage_reporter",
            "mode": "usage_reporting",
        }
        response2 = self.client.post(url, payload2, format="json")
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)

        # Verify both services exist
        self.assertEqual(models.AgentService.objects.count(), 2)
        service_names = list(models.AgentService.objects.values_list("name", flat=True))
        self.assertIn(payload1["name"], service_names)
        self.assertIn(payload2["name"], service_names)

    @data("offering_admin", "admin", "manager", "global_support")
    def test_register_service_forbidden(self, user_role):
        """Test that forbidden roles cannot register services."""
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)

        url = self._get_register_service_url()

        payload = {
            "name": "test_service",
            "mode": "test_mode",
        }

        response = self.client.post(url, payload, format="json")
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
            response.json(),
        )

        # Verify service was not created
        self.assertEqual(models.AgentService.objects.count(), 0)

    def test_register_service_missing_name(self):
        """Test validation error when name is missing."""
        user = self.fixture.staff
        self.client.force_login(user)

        url = self._get_register_service_url()

        # Missing "name" field
        payload = {
            "mode": "test_mode",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify error message
        response_data = response.json()
        self.assertIn("name", response_data)

        # Verify service was not created
        self.assertEqual(models.AgentService.objects.count(), 0)

    def test_register_service_unauthenticated(self):
        """Test that unauthenticated users cannot register services."""
        url = self._get_register_service_url()

        payload = {
            "name": "test_service",
            "mode": "test_mode",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # Verify service was not created
        self.assertEqual(models.AgentService.objects.count(), 0)

    def test_register_service_nonexistent_identity(self):
        """Test 404 error for non-existent agent identity."""
        user = self.fixture.staff
        self.client.force_login(user)

        # Use a non-existent UUID
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        url = f"/api/marketplace-site-agent-identities/{fake_uuid}/register_service/"

        payload = {
            "name": "test_service",
            "mode": "test_mode",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class IdentityManagerAgentIdentityTest(test.APITestCase):
    """Identity managers can create agent identities for non-archived/draft offerings
    and manage only their own."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        self.identity_manager = UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:efp"],
        )

        self.other_identity_manager = UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:fenix"],
        )

        self.payload = {
            "name": "Agent from IdM",
            "offering": self.offering.uuid.hex,
        }

    def test_identity_manager_can_create_without_offering_users(self):
        """ISD manager can create agent identity even without any offering users."""
        self.client.force_authenticate(user=self.identity_manager)
        url = factories.AgentIdentityFactory.get_list_url()
        response = self.client.post(url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        agent = models.AgentIdentity.objects.get(name="Agent from IdM")
        self.assertEqual(agent.created_by, self.identity_manager)

    def test_other_identity_manager_can_also_create(self):
        """Any ISD manager with managed_isds can create, regardless of ISD values."""
        self.client.force_authenticate(user=self.other_identity_manager)
        url = factories.AgentIdentityFactory.get_list_url()
        response = self.client.post(url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_identity_manager_cannot_create_for_archived_offering(self):
        self.offering.state = enums.OfferingStates.ARCHIVED
        self.offering.save()
        self.client.force_authenticate(user=self.identity_manager)
        url = factories.AgentIdentityFactory.get_list_url()
        response = self.client.post(url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_identity_manager_cannot_create_for_draft_offering(self):
        self.offering.state = enums.OfferingStates.DRAFT
        self.offering.save()
        self.client.force_authenticate(user=self.identity_manager)
        url = factories.AgentIdentityFactory.get_list_url()
        response = self.client.post(url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_identity_manager_can_destroy_own_agent_identity(self):
        agent_identity = factories.AgentIdentityFactory(
            offering=self.offering,
            name="My Agent",
            created_by=self.identity_manager,
        )
        self.client.force_authenticate(user=self.identity_manager)
        url = factories.AgentIdentityFactory.get_url(agent_identity)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_identity_manager_cannot_destroy_others_agent_identity(self):
        agent_identity = factories.AgentIdentityFactory(
            offering=self.offering,
            name="Other's Agent",
            created_by=self.other_identity_manager,
        )
        self.client.force_authenticate(user=self.identity_manager)
        url = factories.AgentIdentityFactory.get_url(agent_identity)
        response = self.client.delete(url)
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    def test_identity_manager_can_list_own_agent_identities(self):
        factories.AgentIdentityFactory(
            offering=self.offering,
            name="My Agent",
            created_by=self.identity_manager,
        )
        factories.AgentIdentityFactory(
            offering=self.offering,
            name="Other's Agent",
            created_by=self.other_identity_manager,
        )
        self.client.force_authenticate(user=self.identity_manager)
        url = factories.AgentIdentityFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "My Agent")
