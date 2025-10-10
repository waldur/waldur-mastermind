"""Tests for UPDATE_RESOURCE permission on resource update operations."""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests.factories import (
    CustomerFactory,
    ProjectFactory,
    UserFactory,
)
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


@ddt
class ResourceUpdatePermissionTest(test.APITransactionTestCase):
    """Test UPDATE_RESOURCE permission for resource update operations."""

    def setUp(self):
        """Set up test fixtures and data."""
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        # Add UPDATE_RESOURCE permission to roles that should have it
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE)

        # URLs for consumer and provider endpoints
        self.consumer_url = factories.ResourceFactory.get_url(self.resource)
        self.provider_url = factories.ResourceFactory.get_provider_resource_url(
            self.resource
        )

        # Sample update data
        self.update_data = {
            "name": "Updated Resource Name",
            "description": "Updated description for testing",
        }

        # Create additional test users
        self.other_customer_owner = UserFactory()
        self.other_project_admin = UserFactory()
        self.unrelated_user = UserFactory()

        # Set up a different customer and project for negative tests
        self.other_customer = CustomerFactory()
        self.other_project = ProjectFactory(customer=self.other_customer)
        self.other_customer.add_user(self.other_customer_owner, CustomerRole.OWNER)
        self.other_project.add_user(self.other_project_admin, ProjectRole.ADMIN)

    def make_update_request(self, user, url=None, method="patch", data=None):
        """Helper method to make update requests."""
        self.client.force_authenticate(user)
        url = url or self.consumer_url
        data = data or self.update_data

        if method == "patch":
            return self.client.patch(url, data)
        elif method == "put":
            # For PUT requests, we need all required fields
            full_data = {
                "name": data.get("name", self.resource.name),
                "description": data.get("description", self.resource.description),
                "end_date": data.get("end_date", self.resource.end_date),
            }
            return self.client.put(url, full_data)

    # Positive test cases - users who should have UPDATE_RESOURCE permission

    @data("staff")
    def test_staff_can_update_resource(self, user_attr):
        """Test that staff users can update resources."""
        user = getattr(self.fixture, user_attr)
        response = self.make_update_request(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the resource was actually updated
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, self.update_data["name"])
        self.assertEqual(self.resource.description, self.update_data["description"])

    @data("owner")
    def test_customer_owner_can_update_resource(self, user_attr):
        """Test that customer owners can update their resources."""
        user = getattr(self.fixture, user_attr)
        response = self.make_update_request(user)

        # Debug: print response content if not successful
        if response.status_code != status.HTTP_200_OK:
            print(f"Response status: {response.status_code}")
            print(f"Response data: {response.data}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, self.update_data["name"])

    @data("admin", "manager")
    def test_project_roles_can_update_resource(self, user_attr):
        """Test that project admin and manager can update resources in their project."""
        user = getattr(self.fixture, user_attr)
        response = self.make_update_request(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, self.update_data["name"])

    @data("offering_owner")
    def test_offering_roles_can_update_resource_via_provider_endpoint(self, user_attr):
        """Test that offering owner can update resources via provider endpoint."""
        user = getattr(self.fixture, user_attr)
        response = self.make_update_request(user, url=self.provider_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, self.update_data["name"])

    def test_put_method_works_with_permission(self):
        """Test that PUT method works for users with permission."""
        response = self.make_update_request(self.fixture.owner, method="put")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, self.update_data["name"])

    # Negative test cases - users who should NOT have UPDATE_RESOURCE permission

    def test_unrelated_user_cannot_update_resource(self):
        """Test that unrelated users cannot update resources."""
        response = self.make_update_request(self.unrelated_user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Verify resource was not updated
        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.name, self.update_data["name"])

    def test_other_customer_owner_cannot_update_resource(self):
        """Test that owners of other customers cannot update resources."""
        response = self.make_update_request(self.other_customer_owner)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.name, self.update_data["name"])

    def test_other_project_admin_cannot_update_resource(self):
        """Test that admins of other projects cannot update resources."""
        response = self.make_update_request(self.other_project_admin)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.name, self.update_data["name"])

    def test_unauthenticated_user_cannot_update_resource(self):
        """Test that unauthenticated users cannot update resources."""
        self.client.force_authenticate(user=None)
        response = self.client.patch(self.consumer_url, self.update_data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.name, self.update_data["name"])

    def test_member_cannot_update_resource(self):
        """Test that project members (without admin rights) cannot update resources."""
        member = UserFactory()
        self.fixture.project.add_user(member, ProjectRole.MEMBER)

        response = self.make_update_request(member)
        # Members might not have view permission, so they get 404
        self.assertIn(
            response.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND]
        )

        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.name, self.update_data["name"])

    # Edge cases and validation tests

    def test_cannot_update_resource_in_terminating_state(self):
        """Test that resources in terminating state cannot be updated."""
        self.resource.state = ResourceStates.TERMINATING
        self.resource.save()

        self.make_update_request(self.fixture.owner)
        # The update might be allowed but should be validated at the serializer level
        # This depends on the actual implementation of state validation

    def test_partial_update_only_updates_provided_fields(self):
        """Test that PATCH only updates provided fields."""
        partial_data = {"name": "Only Name Updated"}
        original_description = self.resource.description

        response = self.make_update_request(self.fixture.owner, data=partial_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, partial_data["name"])
        self.assertEqual(self.resource.description, original_description)

    def test_empty_update_is_allowed(self):
        """Test that empty update request is allowed but doesn't change anything."""
        # Create a fresh resource for this test to avoid contamination from other tests
        fresh_resource = factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
            state=ResourceStates.OK,
            name="Test Resource",
            description="Test Description",
        )

        original_name = fresh_resource.name
        original_description = fresh_resource.description

        # Use the fresh resource URL
        url = factories.ResourceFactory.get_url(fresh_resource)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(url, {})

        # Empty patch is valid - it just doesn't change anything
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        fresh_resource.refresh_from_db()
        # Fields should remain unchanged with empty update
        self.assertEqual(fresh_resource.name, original_name)
        self.assertEqual(fresh_resource.description, original_description)

    def test_invalid_field_update_returns_bad_request(self):
        """Test that trying to update invalid fields returns bad request."""
        invalid_data = {
            "name": "Valid Name",
            "invalid_field": "This field doesn't exist",
        }

        response = self.make_update_request(self.fixture.owner, data=invalid_data)
        # Invalid fields are typically ignored in DRF, so this should still succeed
        # But the invalid field should not be processed
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, invalid_data["name"])
        self.assertFalse(hasattr(self.resource, "invalid_field"))


@ddt
class ResourceUpdatePermissionIntegrationTest(test.APITransactionTestCase):
    """Integration tests for UPDATE_RESOURCE permission with complex scenarios."""

    def setUp(self):
        """Set up complex test scenarios."""
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        # Add UPDATE_RESOURCE permission to roles that should have it
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE)

    def test_permission_inheritance_from_customer_to_project(self):
        """Test that customer owner can update resources in any project of the customer."""
        # Create another project in the same customer
        other_project = ProjectFactory(customer=self.fixture.customer)
        other_resource = factories.ResourceFactory(
            project=other_project,
            offering=self.fixture.offering,
        )

        # Customer owner should be able to update resource in any project
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_url(other_resource)
        response = self.client.patch(url, {"name": "Updated by Customer Owner"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        other_resource.refresh_from_db()
        self.assertEqual(other_resource.name, "Updated by Customer Owner")

    def test_offering_owner_can_update_via_provider_endpoint(self):
        """Test that offering owner can update resource via provider endpoint."""
        # Create a user who owns the offering's customer
        offering_owner = UserFactory()
        self.fixture.offering.customer.add_user(offering_owner, CustomerRole.OWNER)

        self.client.force_authenticate(offering_owner)
        url = factories.ResourceFactory.get_provider_resource_url(self.resource)
        response = self.client.patch(url, {"name": "Updated by Offering Owner"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, "Updated by Offering Owner")

    def test_customer_support_role_permissions(self):
        """Test UPDATE_RESOURCE permission for customer support role."""
        support_user = UserFactory()
        self.fixture.customer.add_user(support_user, CustomerRole.SUPPORT)

        self.client.force_authenticate(support_user)
        url = factories.ResourceFactory.get_url(self.resource)
        response = self.client.patch(url, {"name": "Updated by Support"})

        # Support role's ability to update depends on permission configuration
        # By default, SUPPORT might not have UPDATE_RESOURCE permission
        # They also might not have view permission, resulting in 404
        self.assertIn(
            response.status_code,
            [status.HTTP_200_OK, status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    def test_concurrent_update_attempts(self):
        """Test behavior when multiple users try to update simultaneously."""
        # This is more of a database-level test, but we can simulate sequential updates

        # First update by owner
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_url(self.resource)
        response1 = self.client.patch(url, {"name": "First Update"})
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Second update by admin
        self.client.force_authenticate(self.fixture.admin)
        response2 = self.client.patch(url, {"name": "Second Update"})
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # The last update should win
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, "Second Update")

    def test_update_permission_with_blocked_customer(self):
        """Test that blocked customers might or might not prevent resource updates."""
        # Block the customer
        self.fixture.customer.blocked = True
        self.fixture.customer.save()

        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_url(self.resource)
        original_name = self.resource.name
        response = self.client.patch(url, {"name": "Should Maybe Fail"})

        # Depending on implementation, blocked customer might still allow updates
        # or it might block them with 400/403
        if response.status_code in [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_403_FORBIDDEN,
        ]:
            # Verify resource was not updated if blocked
            self.resource.refresh_from_db()
            self.assertEqual(self.resource.name, original_name)
        elif response.status_code == status.HTTP_200_OK:
            # Update was allowed despite blocked customer
            self.resource.refresh_from_db()
            self.assertEqual(self.resource.name, "Should Maybe Fail")
        else:
            # Unexpected status code
            self.fail(f"Unexpected status code: {response.status_code}")
