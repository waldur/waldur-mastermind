"""Test that customer user count is accurate and handles overlapping roles correctly."""

from django.test import TestCase
from django.test.utils import override_settings
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


@override_settings(DEBUG=True)
class CustomerUserCountAccuracyTest(TestCase):
    """Test that customer user counting handles role overlap correctly."""

    def setUp(self):
        """Set up test data with overlapping user roles."""
        # Use the existing fixture which sets up roles properly
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project1 = self.fixture.project
        self.project2 = structure_factories.ProjectFactory(customer=self.customer)

        # Create users with different role combinations
        self.customer_only_user = structure_factories.UserFactory()
        self.project_only_user = structure_factories.UserFactory()
        self.overlap_user = (
            structure_factories.UserFactory()
        )  # Has both customer and project roles
        self.multi_project_user = (
            structure_factories.UserFactory()
        )  # Has roles in multiple projects

        # Use the available roles from fixture
        from waldur_core.permissions.fixtures import CustomerRole, ProjectRole

        # Customer-only user
        self.customer.add_user(self.customer_only_user, CustomerRole.OWNER)

        # Project-only user (project1)
        self.project1.add_user(self.project_only_user, ProjectRole.ADMIN)

        # Overlap user (customer owner + project admin in project1)
        self.customer.add_user(self.overlap_user, CustomerRole.OWNER)
        self.project1.add_user(self.overlap_user, ProjectRole.ADMIN)

        # Multi-project user (admin in project1, manager in project2)
        self.project1.add_user(self.multi_project_user, ProjectRole.ADMIN)
        self.project2.add_user(self.multi_project_user, ProjectRole.MANAGER)

        self.client = test.APIClient()

    def test_customer_user_count_no_double_counting(self):
        """Test that users with multiple roles are counted only once."""
        # Authenticate as a staff user to see all customers
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        url = "/api/customers/"
        params = {
            "field": ["uuid", "name", "users_count"],
        }

        response = self.client.get(url, params)

        # Check response is successful
        self.assertEqual(response.status_code, 200)

        # Find our customer in the response
        customer_data = None
        if isinstance(response.data, dict) and "results" in response.data:
            customers = response.data["results"]
        else:
            customers = response.data

        for customer in customers:
            if customer["uuid"] == str(self.customer.uuid):
                customer_data = customer
                break

        self.assertIsNotNone(customer_data, "Customer not found in response")

        # Expected count:
        # - customer_only_user: 1
        # - project_only_user: 1
        # - overlap_user: 1 (counted once despite having both customer and project roles)
        # - multi_project_user: 1 (counted once despite having roles in multiple projects)
        # Total: 4 unique users
        expected_count = 4
        actual_count = customer_data["users_count"]

        self.assertEqual(
            actual_count,
            expected_count,
            f"Expected {expected_count} unique users, got {actual_count}. "
            f"This suggests double counting of users with overlapping roles.",
        )

    def test_customer_user_count_with_inactive_roles(self):
        """Test that inactive roles are not counted."""
        # Create a user with an inactive role
        inactive_user = structure_factories.UserFactory()
        from waldur_core.permissions.fixtures import CustomerRole

        # Add user with active role first
        self.customer.add_user(inactive_user, CustomerRole.OWNER)

        # Then deactivate the role
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import UserRole

        customer_ct = ContentType.objects.get_for_model(self.customer)
        user_role = UserRole.objects.get(
            user=inactive_user,
            content_type=customer_ct,
            object_id=self.customer.id,
            role=CustomerRole.OWNER,
        )
        user_role.is_active = False
        user_role.save()

        # Test count
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        url = "/api/customers/"
        params = {
            "field": ["uuid", "name", "users_count"],
        }

        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)

        # Find our customer in the response
        customer_data = None
        if isinstance(response.data, dict) and "results" in response.data:
            customers = response.data["results"]
        else:
            customers = response.data

        for customer in customers:
            if customer["uuid"] == str(self.customer.uuid):
                customer_data = customer
                break

        self.assertIsNotNone(customer_data)

        # Should still be 4 (inactive user should not be counted)
        expected_count = 4
        actual_count = customer_data["users_count"]

        self.assertEqual(
            actual_count,
            expected_count,
            f"Expected {expected_count} users (inactive role should not be counted), got {actual_count}",
        )

    def test_customer_user_count_field_not_requested(self):
        """Test that users_count is not calculated when field is not requested."""
        from django.db import connection, reset_queries

        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        reset_queries()

        url = "/api/customers/"
        params = {
            "field": ["uuid", "name"],  # No users_count field
        }

        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)

        # Check that no UserRole queries were made for counting
        user_role_queries = [
            query
            for query in connection.queries
            if "permissions_userrole" in query["sql"].lower()
            and "count" in query["sql"].lower()
        ]

        self.assertEqual(
            len(user_role_queries),
            0,
            f"Expected no user counting queries when users_count field not requested, "
            f"but found {len(user_role_queries)} queries",
        )
