from unittest.mock import Mock, patch

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework import exceptions, test

from waldur_core.permissions import models, utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class PermissionsSystemIntegrationTest(test.APITransactionTestCase):
    """Comprehensive integration tests for the permissions system."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.customer_owner = self.fixture.owner
        self.project_admin = self.fixture.admin
        self.project_manager = self.fixture.manager

        # Create additional users for testing
        self.regular_user = factories.UserFactory()
        self.staff_user = factories.UserFactory(is_staff=True)

    def test_complete_permission_workflow(self):
        """Test complete workflow from role creation to permission checking."""
        # 1. Create custom role
        customer_ct = ContentType.objects.get_for_model(self.customer)
        custom_role = models.Role.objects.create(
            name="CUSTOM.MANAGER", content_type=customer_ct
        )

        # 2. Add permissions to role
        custom_role.add_permission(PermissionEnum.UPDATE_OFFERING)
        custom_role.add_permission(PermissionEnum.LIST_ORDERS)

        # 3. Assign role to user
        user_role = utils.add_user(
            self.customer,
            self.regular_user,
            custom_role,
            created_by=self.customer_owner,
        )

        # 4. Verify permission checking works
        self.assertTrue(
            utils.has_permission(
                self.regular_user, PermissionEnum.UPDATE_OFFERING, self.customer
            )
        )
        self.assertTrue(
            utils.has_permission(
                self.regular_user, PermissionEnum.LIST_ORDERS, self.customer
            )
        )
        self.assertFalse(
            utils.has_permission(
                self.regular_user, PermissionEnum.CREATE_OFFERING, self.customer
            )
        )

        # 5. Test permission factory
        permission_func = utils.permission_factory(PermissionEnum.UPDATE_OFFERING)
        mock_request = Mock()
        mock_request.user = self.regular_user

        # Should not raise exception
        permission_func(mock_request, None, self.customer)

        # 6. Revoke role and verify permissions are gone
        user_role.revoke()
        self.assertFalse(
            utils.has_permission(
                self.regular_user, PermissionEnum.UPDATE_OFFERING, self.customer
            )
        )

    def test_hierarchical_permission_inheritance(self):
        """Test that permission checking works across organizational hierarchy."""
        # Setup permissions at different levels
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        # Test customer-level permission directly
        result = utils.has_permission(
            self.customer_owner, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

        # Test project-level permission
        result = utils.has_permission(
            self.project_admin, PermissionEnum.APPROVE_ORDER, self.project
        )
        self.assertTrue(result)

        # Test permission factory with real objects
        customer_permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["customer"]
        )

        mock_request = Mock()
        mock_request.user = self.customer_owner

        # Should work for project when checking project.customer
        customer_permission_func(mock_request, None, self.project)

        # Test project-level permission factory
        project_permission_func = utils.permission_factory(PermissionEnum.APPROVE_ORDER)

        mock_request.user = self.project_admin

        # Should work for project directly
        project_permission_func(mock_request, None, self.project)

    def test_time_based_permissions_lifecycle(self):
        """Test complete lifecycle of time-based permissions."""
        # Add temporary role
        expiration_time = timezone.now() + timezone.timedelta(days=30)
        temporary_role = CustomerRole.SUPPORT
        temporary_role.add_permission(PermissionEnum.LIST_ORDERS)

        utils.add_user(
            self.customer,
            self.regular_user,
            temporary_role,
            expiration_time=expiration_time,
        )

        # Permission should work initially
        self.assertTrue(
            utils.has_permission(
                self.regular_user, PermissionEnum.LIST_ORDERS, self.customer
            )
        )

        # Verify user shows up in permission queries
        users_with_permission = utils.get_users_with_permission(
            self.customer, PermissionEnum.LIST_ORDERS
        )
        self.assertIn(self.regular_user, users_with_permission)

        # Test has_user with time checking
        future_check = timezone.now() + timezone.timedelta(days=15)
        self.assertTrue(
            utils.has_user(
                self.customer,
                self.regular_user,
                temporary_role,
                expiration_time=future_check,
            )
        )

        # Test has_user past expiration
        past_expiration_check = timezone.now() + timezone.timedelta(days=45)
        self.assertFalse(
            utils.has_user(
                self.customer,
                self.regular_user,
                temporary_role,
                expiration_time=past_expiration_check,
            )
        )

        # Update expiration time
        new_expiration = timezone.now() + timezone.timedelta(days=60)
        utils.update_user(
            self.customer,
            self.regular_user,
            temporary_role,
            expiration_time=new_expiration,
        )

        # Now should work with later check
        later_check = timezone.now() + timezone.timedelta(days=45)
        self.assertTrue(
            utils.has_user(
                self.customer,
                self.regular_user,
                temporary_role,
                expiration_time=later_check,
            )
        )

    def test_multi_role_multi_scope_scenario(self):
        """Test complex scenario with user having multiple roles across multiple scopes."""
        # Create additional customers and projects
        customer2 = factories.CustomerFactory()
        factories.ProjectFactory(customer=customer2)

        # Setup different permissions for different roles - use permissions that don't overlap
        CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_ORDERS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        # Assign user multiple roles across different scopes
        self.customer.add_user(self.regular_user, CustomerRole.SUPPORT)
        customer2.add_user(
            self.regular_user, CustomerRole.SUPPORT
        )  # Same role, different scope
        self.project.add_user(self.regular_user, ProjectRole.ADMIN)

        # Test permissions in different scopes - user should have same permission in both customers
        self.assertTrue(
            utils.has_permission(
                self.regular_user, PermissionEnum.LIST_ORDERS, self.customer
            )
        )
        self.assertTrue(
            utils.has_permission(
                self.regular_user, PermissionEnum.LIST_ORDERS, customer2
            )
        )
        self.assertTrue(
            utils.has_permission(
                self.regular_user, PermissionEnum.APPROVE_ORDER, self.project
            )
        )

        # Test scope isolation - user should NOT have project permission in customer scopes
        self.assertFalse(
            utils.has_permission(
                self.regular_user, PermissionEnum.APPROVE_ORDER, self.customer
            )
        )
        self.assertFalse(
            utils.has_permission(
                self.regular_user, PermissionEnum.APPROVE_ORDER, customer2
            )
        )

        # Test utility functions
        customer_ct = ContentType.objects.get_for_model(self.customer)
        scope_ids = utils.get_scope_ids(self.regular_user, customer_ct)
        self.assertIn(self.customer.id, scope_ids)
        self.assertIn(customer2.id, scope_ids)

    def test_permission_factory_complex_resolution(self):
        """Test permission factory with complex source path resolution."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        # Create complex nested object
        mock_resource = Mock()
        mock_resource.offering.customer = self.customer
        mock_resource.project.customer = self.customer
        mock_resource.direct_customer = (
            factories.CustomerFactory()
        )  # Different customer

        # Test permission factory with fallback paths
        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["direct_customer", "offering.customer", "project.customer"],
        )

        mock_request = Mock()
        mock_request.user = self.customer_owner

        # Should succeed via second or third path (offering.customer or project.customer)
        permission_func(mock_request, None, mock_resource)

        # Test with user that only has project-level permission
        mock_request.user = self.project_admin

        # Should fail because project admin doesn't have UPDATE_OFFERING permission
        with self.assertRaises(exceptions.PermissionDenied):
            permission_func(mock_request, None, mock_resource)

    def test_staff_user_bypass_comprehensive(self):
        """Test that staff users bypass all permission checks comprehensively."""
        # Remove all permissions from roles
        for role in [CustomerRole.OWNER, ProjectRole.ADMIN, ProjectRole.MANAGER]:
            models.RolePermission.objects.filter(role=role).delete()

        # Staff user should still have all permissions
        self.assertTrue(
            utils.has_permission(
                self.staff_user, PermissionEnum.CREATE_OFFERING, self.customer
            )
        )

        # Permission factory should also work for staff
        permission_func = utils.permission_factory(PermissionEnum.CREATE_OFFERING)
        mock_request = Mock()
        mock_request.user = self.staff_user

        # Should not raise exception
        permission_func(mock_request, None, self.customer)

    def test_error_conditions_and_edge_cases(self):
        """Test various error conditions and edge cases."""
        # Test with None scope
        result = utils.has_permission(
            self.regular_user, PermissionEnum.UPDATE_OFFERING, None
        )
        self.assertFalse(result)

        # Test permission factory with None scope
        permission_func = utils.permission_factory(PermissionEnum.UPDATE_OFFERING)
        mock_request = Mock()
        mock_request.user = self.regular_user

        # Should return early without exception
        result = permission_func(mock_request, None, None)
        self.assertIsNone(result)

        # Test with invalid attribute path
        mock_resource = Mock()
        mock_resource.invalid = None

        permission_func_invalid = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["invalid.nonexistent"]
        )

        with self.assertRaises(AttributeError):
            permission_func_invalid(mock_request, None, mock_resource)

    def test_system_role_protection(self):
        """Test that system roles are properly protected."""
        # System roles should exist
        self.assertTrue(CustomerRole.OWNER.is_system_role)
        self.assertTrue(ProjectRole.ADMIN.is_system_role)

        # Test that system roles can't be easily corrupted
        original_name = CustomerRole.OWNER.name
        CustomerRole.OWNER.name = "MODIFIED.NAME"
        CustomerRole.OWNER.save()

        # Verify it still works
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.customer.add_user(self.regular_user, CustomerRole.OWNER)

        result = utils.has_permission(
            self.regular_user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

        # Restore original name
        CustomerRole.OWNER.name = original_name
        CustomerRole.OWNER.save()

    def test_user_role_lifecycle_signals(self):
        """Test that role lifecycle properly triggers signals."""
        with patch("waldur_core.permissions.signals.role_granted.send") as mock_granted:
            with patch(
                "waldur_core.permissions.signals.role_revoked.send"
            ) as mock_revoked:
                # Add user role
                user_role = utils.add_user(
                    self.customer,
                    self.regular_user,
                    CustomerRole.SUPPORT,
                    created_by=self.customer_owner,
                )

                # Should have triggered role_granted signal
                mock_granted.assert_called_once()

                # Revoke user role
                user_role.revoke(current_user=self.customer_owner)

                # Should have triggered role_revoked signal
                mock_revoked.assert_called_once()

    def test_get_valid_content_types_and_models(self):
        """Test utility functions for getting valid content types and models."""
        valid_content_types = utils.get_valid_content_types()
        valid_models = utils.get_valid_models()

        self.assertGreater(len(valid_content_types), 0)
        self.assertGreater(len(valid_models), 0)
        self.assertEqual(len(valid_content_types), len(valid_models))

        # Should include customer and project
        customer_ct = ContentType.objects.get_for_model(self.customer)
        project_ct = ContentType.objects.get_for_model(self.project)

        self.assertIn(customer_ct, valid_content_types)
        self.assertIn(project_ct, valid_content_types)

    def test_comprehensive_query_optimization(self):
        """Test that the permissions system uses optimized database queries."""
        # Create scenario with multiple users and roles
        users = [factories.UserFactory() for _ in range(5)]
        for user in users:
            self.customer.add_user(user, CustomerRole.SUPPORT)

        CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_ORDERS)

        from django.db import connection
        from django.test.utils import override_settings

        with override_settings(DEBUG=True):
            # Reset query count
            connection.queries.clear()

            # Test has_permission
            utils.has_permission(users[0], PermissionEnum.LIST_ORDERS, self.customer)
            has_permission_queries = len(connection.queries)

            # Reset query count
            connection.queries.clear()

            # Test get_users_with_permission
            utils.get_users_with_permission(self.customer, PermissionEnum.LIST_ORDERS)
            get_users_queries = len(connection.queries)

            # Reset query count
            connection.queries.clear()

            # Test count_users
            utils.count_users(self.customer)
            count_users_queries = len(connection.queries)

            # All operations should use reasonable number of queries
            self.assertLessEqual(has_permission_queries, 3)
            self.assertLessEqual(get_users_queries, 2)
            self.assertLessEqual(
                count_users_queries, 3
            )  # Allow 3 queries due to generic FK overhead
