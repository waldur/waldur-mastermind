import uuid

import respx
from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_remote.exceptions import RemoteStatusSyncFailed
from waldur_mastermind.marketplace_remote.tests import fixtures
from waldur_mastermind.marketplace_remote.utils import get_resource_sync_status


class GetResourceSyncStatusTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.MarketplaceRemoteFixture()

        # Create a resource with remote marketplace offering
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project, offering=self.fixture.offering
        )
        self.resource.offering.type = REMOTE_OFFERING
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.state = ResourceStates.OK
        self.resource.save()

        # Set up API credentials
        self.api_url = "https://example.com"
        self.resource.offering.secret_options = {
            "api_url": self.api_url,
            "token": "valid_token",
        }
        self.resource.offering.save()
        respx.start()

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def mock_marketplace_resource(self, resource_uuid, resource_data):
        """Mock the marketplace resource retrieve endpoint"""
        return respx.get(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/"
        ).respond(200, json=resource_data)

    def test_successful_sync_status_retrieval(self):
        """Test successful retrieval of resource sync status"""
        remote_resource_data = {"state": "OK"}
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        result = get_resource_sync_status(self.resource)

        self.assertEqual(result["local_state"], "OK")
        self.assertEqual(result["remote_state"], "OK")
        self.assertEqual(result["sync_status"], "in_sync")
        self.assertIsNotNone(result["last_sync"])

        self.assertTrue(
            respx.get(
                f"{self.api_url}/api/marketplace-resources/{self.resource.backend_id}/"
            ).called
        )

    def test_out_of_sync_status_detection(self):
        """Test detection of out-of-sync resources"""
        remote_resource_data = {"state": "Creating"}
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        result = get_resource_sync_status(self.resource)

        self.assertEqual(result["local_state"], "OK")
        self.assertEqual(result["remote_state"], "Creating")
        self.assertEqual(result["sync_status"], "out_of_sync")
        self.assertIsNotNone(result["last_sync"])

    def test_remote_resource_state_unavailable(self):
        """Test handling when remote resource state is not available"""
        remote_resource_data = {}  # No state field
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        result = get_resource_sync_status(self.resource)

        self.assertEqual(result["local_state"], "OK")
        self.assertIsNone(result["remote_state"])
        self.assertEqual(result["sync_status"], "sync_failed")
        self.assertIn("error", result)
        self.assertIsNone(result["last_sync"])

    def test_api_error_handling(self):
        """Test handling of API errors"""
        respx.get(
            f"{self.api_url}/api/marketplace-resources/{self.resource.backend_id}/"
        ).respond(500, json={"error": "Internal server error"})

        with self.assertRaises(RemoteStatusSyncFailed):
            get_resource_sync_status(self.resource)


class RemoteResourceStatusEndpointTest(test.APITransactionTestCase):
    """Test the new remote resource status endpoint"""

    def setUp(self):
        super().setUp()
        self.fixture = fixtures.MarketplaceRemoteFixture()

        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project, offering=self.fixture.offering
        )
        self.resource.offering.type = REMOTE_OFFERING
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.resource_status_url = (
            f"/api/remote-waldur-api/remote_resource_status/{self.resource.uuid}/"
        )
        self.api_url = "https://example.com"
        self.resource.offering.secret_options = {
            "api_url": self.api_url,
            "token": "valid_token",
        }
        self.resource.offering.save()

        respx.start()

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def mock_marketplace_resource(self, resource_uuid, resource_data):
        """Mock the marketplace resource retrieve endpoint"""
        return respx.get(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/"
        ).respond(200, json=resource_data)

    def test_staff_user_can_access_remote_status(self):
        """Test that staff users can access remote resource status"""
        remote_resource_data = {"state": "OK"}
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.resource_status_url)

        self.assertEqual(response.status_code, 200)
        response_data = response.data

        self.assertEqual(response_data["local_state"], "OK")
        self.assertEqual(response_data["remote_state"], "OK")
        self.assertEqual(response_data["sync_status"], "in_sync")
        self.assertIsNotNone(response_data["last_sync"])

        self.assertTrue(
            respx.get(
                f"{self.api_url}/api/marketplace-resources/{self.resource.backend_id}/"
            ).called
        )

    def test_project_admin_can_access_remote_status(self):
        """Test that project admin users can access remote resource status"""
        remote_resource_data = {"state": "OK"}
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.get(self.resource_status_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sync_status"], "in_sync")

    def test_project_manager_can_access_remote_status(self):
        """Test that project manager users can access remote resource status"""
        remote_resource_data = {"state": "OK"}
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        # Act - call the endpoint as project manager
        self.client.force_authenticate(user=self.fixture.manager)
        response = self.client.get(self.resource_status_url)

        # Assert
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sync_status"], "in_sync")

    def test_project_member_can_access_remote_status(self):
        """Test that project member users can access remote resource status"""
        remote_resource_data = {"state": "OK"}
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        self.client.force_authenticate(user=self.fixture.member)
        response = self.client.get(self.resource_status_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sync_status"], "in_sync")

    def test_customer_owner_can_access_remote_status(self):
        """Test that customer owner users can access remote resource status"""
        remote_resource_data = {"state": "OK"}
        self.mock_marketplace_resource(self.resource.backend_id, remote_resource_data)

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.get(self.resource_status_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sync_status"], "in_sync")

    def test_unauthorized_user_cannot_access_remote_status(self):
        """Test that unauthorized users cannot access remote resource status"""
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.get(self.resource_status_url)

        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_user_cannot_access_remote_status(self):
        """Test that unauthenticated users cannot access remote resource status"""
        response = self.client.get(self.resource_status_url)

        self.assertEqual(response.status_code, 401)

    def test_nonexistent_resource_returns_404(self):
        """Test that requesting status for nonexistent resource returns 404"""
        fake_uuid = uuid.uuid4()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            f"/api/remote-waldur-api/resource_status/{fake_uuid}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_remote_api_error_handling(self):
        """Test that remote API errors are handled gracefully"""
        respx.get(
            f"{self.api_url}/api/marketplace-resources/{self.resource.backend_id}/"
        ).respond(500, json={"error": "Internal server error"})

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.resource_status_url)

        self.assertEqual(response.status_code, 502)
        self.assertIn(
            "Unable to fetch remote resource state for resource",
            response.data["error_message"],
        )
        self.assertIn("error_traceback", response.data)


class RemoteResourceTeamEndpointTest(test.APITransactionTestCase):
    """Test the new remote resource team endpoint"""

    def setUp(self):
        super().setUp()
        self.fixture = fixtures.MarketplaceRemoteFixture()

        # Create resource in the main project where fixture users have access
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project, offering=self.fixture.offering
        )
        self.resource.offering.type = REMOTE_OFFERING
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.state = ResourceStates.OK
        self.resource.save()

        # Set up API credentials
        self.api_url = "https://example.com"
        self.resource.offering.secret_options = {
            "api_url": self.api_url,
            "token": "valid_token",
        }
        self.resource.offering.save()

        self.john_user = structure_factories.UserFactory(
            full_name="John Doe", username="john.doe"
        )
        self.jane_user = structure_factories.UserFactory(
            full_name="Jane Smith", username="jane.smith"
        )

        self.fixture.project.add_user(self.john_user, ProjectRole.ADMIN)
        self.fixture.project.add_user(self.jane_user, ProjectRole.MEMBER)
        self.admin_role_str = ProjectRole.ADMIN.name
        self.member_role_str = ProjectRole.MEMBER.name
        self.resource_team_url = (
            f"/api/remote-waldur-api/remote_resource_team_status/{self.resource.uuid}/"
        )
        respx.start()

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def mock_marketplace_team(self, resource_uuid, team_data=None):
        """Mock the marketplace resource team endpoint"""
        if team_data is None:
            team_data = self.get_default_team_data()
        return respx.get(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/team/"
        ).respond(200, json=team_data)

    def get_default_team_data(self):
        """Get default team data for testing"""
        return [
            {
                "uuid": uuid.uuid4().hex,
                "full_name": "John Doe",
                "role": self.admin_role_str,
                "url": "https://example.com/api/project-users/1234567890abcdef1234567890abcdef/",
                "username": "john.doe",
                "expiration_time": "2025-01-01T00:00:00Z",
                "offering_user_username": "john.doe",
                "offering_user_state": "OK",
                "email": "john.doe@example.com",
            },
            {
                "uuid": uuid.uuid4().hex,
                "full_name": "Jane Smith",
                "role": self.member_role_str,
                "url": "https://example.com/api/project-users/abcdef1234567890abcdef1234567890/",
                "username": "jane.smith",
                "expiration_time": "2025-01-01T00:00:00Z",
                "offering_user_username": "jane.smith",
                "offering_user_state": "OK",
                "email": "jane.smith@example.com",
            },
        ]

    def test_staff_user_can_access_remote_team(self):
        """Test that staff users can access remote resource team"""
        self.mock_marketplace_team(self.resource.backend_id)

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 200)
        response_data = response.data

        # Should return team data with sync status
        self.assertEqual(len(response_data), 2)
        self.assertEqual(response_data[1]["full_name"], "John Doe")
        self.assertEqual(response_data[1]["remote_role"], self.admin_role_str)
        self.assertEqual(response_data[1]["sync_status"], "in_sync")

        # Verify API call was made
        self.assertTrue(
            respx.get(
                f"{self.api_url}/api/marketplace-resources/{self.resource.backend_id}/team/"
            ).called
        )

    def test_project_admin_can_access_remote_team(self):
        """Test that project admin users can access remote resource team"""
        self.mock_marketplace_team(self.resource.backend_id)

        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 200)
        # There are 3 users in the team: 2 remote and 1 local (the admin from the fixture)
        self.assertEqual(len(response.data), 3)

    def test_project_manager_can_access_remote_team(self):
        """Test that project manager users can access remote resource team"""
        self.mock_marketplace_team(self.resource.backend_id)

        self.client.force_authenticate(user=self.fixture.manager)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 200)

    def test_project_member_can_access_remote_team(self):
        """Test that project member users can access remote resource team"""
        self.mock_marketplace_team(self.resource.backend_id)

        self.client.force_authenticate(user=self.fixture.member)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 200)

    def test_customer_owner_can_access_remote_team(self):
        """Test that customer owner users can access remote resource team"""
        self.mock_marketplace_team(self.resource.backend_id)

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 200)

    def test_unauthorized_user_cannot_access_remote_team(self):
        """Test that unauthorized users cannot access remote resource team"""
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_user_cannot_access_remote_team(self):
        """Test that unauthenticated users cannot access remote resource team"""
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 401)

    def test_ordering_works_correctly(self):
        """Test that ordering works correctly with team data"""
        remote_team_data = [
            {
                "uuid": uuid.uuid4().hex,
                "full_name": "Zebra Smith",
                "role": self.member_role_str,
                "url": "https://example.com/api/project-users/44444444444444444444444444444444/",
                "username": "zebra.smith",
                "expiration_time": "2025-01-01T00:00:00Z",
                "offering_user_username": "zebra.smith",
                "offering_user_state": "OK",
                "email": "zebra.smith@example.com",
            },
            {
                "uuid": uuid.uuid4().hex,
                "full_name": "Alice Johnson",
                "role": self.admin_role_str,
                "url": "https://example.com/api/project-users/55555555555555555555555555555555/",
                "username": "alice.johnson",
                "expiration_time": "2025-01-01T00:00:00Z",
                "offering_user_username": "alice.johnson",
                "offering_user_state": "OK",
                "email": "alice.johnson@example.com",
            },
        ]
        self.mock_marketplace_team(self.resource.backend_id, remote_team_data)

        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.get(self.resource_team_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["full_name"], "Alice Johnson")
        self.assertEqual(response.data[3]["full_name"], "Zebra Smith")
        response = self.client.get(f"{self.resource_team_url}?o=-full_name")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["full_name"], "Zebra Smith")
        self.assertEqual(response.data[3]["full_name"], "Alice Johnson")

    def test_nonexistent_resource_returns_404(self):
        """Test that requesting team for nonexistent resource returns 404"""
        fake_uuid = uuid.uuid4()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_team_status/{fake_uuid}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_remote_api_error_handling(self):
        """Test that remote API errors are handled gracefully"""
        respx.get(
            f"{self.api_url}/api/marketplace-resources/{self.resource.backend_id}/team/"
        ).respond(500, json={"error": "Internal server error"})

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.resource_team_url)
        self.assertEqual(response.status_code, 502)
        self.assertIn(
            "Unable to fetch remote team data for resource",
            response.data["error_message"],
        )
        self.assertIn("error_traceback", response.data)

    def test_empty_team_returns_empty_list(self):
        """Test that empty team returns empty list"""
        remote_team_data = []
        self.mock_marketplace_team(self.resource.backend_id, remote_team_data)

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.resource_team_url)
        self.assertEqual(response.status_code, 200)
        # Assert there are 2 local users
        self.assertEqual(len(response.data), 2)
        # Assert there are 2 sync_failed users (one for each local user)
        self.assertEqual(
            len(
                [user for user in response.data if user["sync_status"] == "sync_failed"]
            ),
            2,
        )

    def test_out_of_sync_status_detection(self):
        """Test that out-of-sync status is detected"""
        remote_team_data = [
            {
                "uuid": uuid.uuid4().hex,
                "full_name": "John Doe",
                "role": self.member_role_str,
                "url": "https://example.com/api/project-users/1234567890abcdef1234567890abcdef/",
                "username": "john.doe",
                "expiration_time": "2025-01-01T00:00:00Z",
                "offering_user_username": "john.doe",
                "offering_user_state": "Requested",
                "email": "john.doe@example.com",
            }
        ]
        self.mock_marketplace_team(self.resource.backend_id, remote_team_data)

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[1]["sync_status"], "out_of_sync")

    def test_not_found_status_detection(self):
        """Test that not found status is detected"""
        remote_team_data = [
            {
                "uuid": uuid.uuid4().hex,
                "full_name": "Not Found",
                "role": self.member_role_str,
                "url": "https://example.com/api/project-users/1234567890abcdef1234567890abcdef/",
                "username": "john.unknown",
                "expiration_time": "2025-01-01T00:00:00Z",
                "offering_user_username": "john.doe",
                "offering_user_state": "Requested",
                "email": "john.doe@example.com",
            }
        ]
        self.mock_marketplace_team(self.resource.backend_id, remote_team_data)

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.resource_team_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[2]["sync_status"], "sync_failed")
        self.assertEqual(response.data[2]["local_role"], "unknown")


class GetResourceOrderSyncStatusTest(test.APITransactionTestCase):
    """Test the get_resource_order_sync_status utility function"""

    def setUp(self):
        super().setUp()
        self.fixture = fixtures.MarketplaceRemoteFixture()

        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project, offering=self.fixture.offering
        )
        self.resource.offering.type = REMOTE_OFFERING
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.api_url = "https://example.com"
        self.resource.offering.secret_options = {
            "api_url": self.api_url,
            "token": "valid_token",
        }
        self.resource.offering.save()
        self.remote_order_uuid_1 = uuid.uuid4().hex
        self.remote_order_uuid_2 = uuid.uuid4().hex
        self.local_order_1 = marketplace_factories.OrderFactory(
            resource=self.resource,
            project=self.fixture.project,
            created_by=self.fixture.admin,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            backend_id=self.remote_order_uuid_1,
        )
        self.local_order_2 = marketplace_factories.OrderFactory(
            resource=self.resource,
            project=self.fixture.project,
            created_by=self.fixture.admin,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
            backend_id=self.remote_order_uuid_2,
        )
        respx.start()

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def mock_marketplace_orders_list(self, resource_uuid, orders_data):
        """Mock the marketplace orders list endpoint"""
        return respx.get(
            f"{self.api_url}/api/marketplace-orders/",
            params={"resource_uuid": resource_uuid},
        ).respond(200, json=orders_data)

    def test_successful_order_sync_status_retrieval(self):
        """Test successful retrieval of order sync status"""
        remote_orders_data = [
            {
                "uuid": self.remote_order_uuid_1,
                "state": "done",
                "type": "Create",
                "created": "2025-01-01T00:00:00Z",
                "modified": "2025-01-01T00:00:00Z",
            }
        ]
        self.mock_marketplace_orders_list(self.resource.backend_id, remote_orders_data)
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_order_status/{self.resource.uuid}/"
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["remote_state"], "done")
        self.assertEqual(response.data[0]["local_state"], "done")
        self.assertEqual(response.data[0]["sync_status"], "in_sync")

        self.assertTrue(
            respx.get(
                f"{self.api_url}/api/marketplace-orders/",
                params={"resource_uuid": self.resource.backend_id},
            ).called
        )

    def test_multiple_orders_sync_status(self):
        """Test sync status for multiple orders"""
        remote_orders_data = [
            {
                "uuid": self.remote_order_uuid_1,
                "state": "done",
                "type": "Create",
                "created": "2025-01-01T00:00:00Z",
                "modified": "2025-01-01T00:00:00Z",
            },
            {
                "uuid": self.remote_order_uuid_2,
                "state": "pending-consumer",
                "type": "Update",
                "created": "2025-01-02T00:00:00Z",
                "modified": "2025-01-02T00:00:00Z",
            },
        ]
        self.mock_marketplace_orders_list(self.resource.backend_id, remote_orders_data)
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_order_status/{self.resource.uuid}/"
        )
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data[0]["remote_state"], "done")
        self.assertEqual(response.data[1]["remote_state"], "pending-consumer")
        self.assertEqual(response.data[0]["local_state"], "done")
        self.assertEqual(response.data[1]["local_state"], "executing")
        self.assertEqual(response.data[1]["sync_status"], "out_of_sync")

    def test_empty_orders_list(self):
        """Test handling when no orders exist"""
        remote_orders_data = []
        self.mock_marketplace_orders_list(self.resource.backend_id, remote_orders_data)
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_order_status/{self.resource.uuid}/"
        )
        self.assertEqual(len(response.data), 0)

    def test_api_error_handling(self):
        """Test handling of API errors"""
        respx.get(
            f"{self.api_url}/api/marketplace-orders/",
            params={"resource_uuid": self.resource.backend_id},
        ).respond(500, json={"error": "Internal server error"})
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_order_status/{self.resource.uuid}/"
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn(
            "Unable to fetch remote order data for resource",
            response.data["error_message"],
        )
        self.assertIn("error_traceback", response.data)

    def test_project_admin_can_access_remote_order_status(self):
        """Test that project admin users can access remote order status"""
        remote_orders_data = [
            {
                "uuid": self.remote_order_uuid_1,
                "state": "done",
                "type": "Create",
                "created": "2025-01-01T00:00:00Z",
                "modified": "2025-01-01T00:00:00Z",
            }
        ]
        self.mock_marketplace_orders_list(self.resource.backend_id, remote_orders_data)
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_order_status/{self.resource.uuid}/"
        )
        self.assertEqual(response.status_code, 200)

    def test_random_user_cannot_access_remote_order_status(self):
        """Test that random users cannot access remote order status"""
        remote_orders_data = [
            {
                "uuid": self.remote_order_uuid_1,
                "state": "done",
                "type": "Create",
                "created": "2025-01-01T00:00:00Z",
                "modified": "2025-01-01T00:00:00Z",
            }
        ]
        self.mock_marketplace_orders_list(self.resource.backend_id, remote_orders_data)
        self.client.force_authenticate(user=structure_factories.UserFactory())
        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_order_status/{self.resource.uuid}/"
        )
        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_user_cannot_access_remote_order_status(self):
        """Test that unauthenticated users cannot access remote order status"""
        response = self.client.get(
            f"/api/remote-waldur-api/remote_resource_order_status/{self.resource.uuid}/"
        )
        self.assertEqual(response.status_code, 401)
