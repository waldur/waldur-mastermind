from unittest.mock import Mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework import exceptions

from waldur_core.permissions import models, utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class HierarchicalPermissionTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.customer_owner = self.fixture.owner  # Has customer owner role
        self.project_admin = self.fixture.admin  # Has project admin role
        self.project_manager = self.fixture.manager  # Has project manager role

    def test_customer_level_permission_inheritance(self):
        """Test that customer-level permissions work across projects."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Customer owner should have permission on customer
        result = utils.has_permission(
            self.customer_owner, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

    def test_project_level_permission_isolation(self):
        """Test that project-level permissions are isolated per project."""
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Create another project
        other_project = factories.ProjectFactory(customer=self.customer)

        # Project admin should have permission on their project
        result = utils.has_permission(
            self.project_admin, PermissionEnum.UPDATE_OFFERING, self.project
        )
        self.assertTrue(result)

        # But not on other project
        result = utils.has_permission(
            self.project_admin, PermissionEnum.UPDATE_OFFERING, other_project
        )
        self.assertFalse(result)

    def test_multiple_roles_permission_aggregation(self):
        """Test that permissions from multiple roles are aggregated."""
        # User has both customer and project roles
        user = factories.UserFactory()

        # Add user as customer manager
        CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_ORDERS)
        self.customer.add_user(user, CustomerRole.SUPPORT)

        # Add user as project admin
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.project.add_user(user, ProjectRole.ADMIN)

        # User should have both permissions in appropriate scopes
        self.assertTrue(
            utils.has_permission(user, PermissionEnum.LIST_ORDERS, self.customer)
        )
        self.assertTrue(
            utils.has_permission(user, PermissionEnum.APPROVE_ORDER, self.project)
        )

    def test_cross_scope_permission_checking(self):
        """Test permission checking across different scope types."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Create a mock offering that belongs to this customer
        mock_offering = Mock()
        mock_offering.customer = self.customer

        # Permission factory should work with nested scope resolution
        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["customer"]
        )

        mock_request = Mock()
        mock_request.user = self.customer_owner

        # Should not raise exception
        permission_func(mock_request, None, mock_offering)


class PermissionFactoryComplexTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = self.fixture.owner

    def test_permission_factory_multiple_source_paths(self):
        """Test permission factory with multiple source path resolution."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Create nested mock object
        mock_resource = Mock()
        mock_resource.project.customer = self.customer
        mock_resource.offering.customer = self.customer

        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["project.customer", "offering.customer"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should succeed via either path
        permission_func(mock_request, None, mock_resource)

    def test_permission_factory_first_path_fails_second_succeeds(self):
        """Test permission factory when first path fails but second succeeds."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Create mock where user has no permission via first path
        mock_resource = Mock()
        mock_resource.other_entity.customer = (
            factories.CustomerFactory()
        )  # No permission
        mock_resource.project.customer = self.customer  # Has permission

        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["other_entity.customer", "project.customer"],
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should succeed via second path
        permission_func(mock_request, None, mock_resource)

    def test_permission_factory_all_paths_fail(self):
        """Test permission factory when all paths fail."""
        # Create a mock object without the required attributes
        mock_object = Mock()
        mock_object.nonexistent_attr = (
            None  # This will cause AttributeError when accessing .customer
        )

        permission_func = utils.permission_factory(
            PermissionEnum.CREATE_OFFERING, ["nonexistent_attr.customer"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should raise AttributeError (which gets converted to PermissionDenied in the end)
        with self.assertRaises((exceptions.PermissionDenied, AttributeError)):
            permission_func(mock_request, None, mock_object)

    def test_permission_factory_deep_nested_path(self):
        """Test permission factory with deeply nested attribute paths."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Create deeply nested mock
        mock_object = Mock()
        mock_object.level1.level2.level3.customer = self.customer

        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["level1.level2.level3.customer"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should resolve deep path successfully
        permission_func(mock_request, None, mock_object)

    def test_permission_factory_attribute_error_handling(self):
        """Test permission factory handling of missing attributes."""
        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["nonexistent.attribute"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should raise AttributeError when attribute doesn't exist (current behavior)
        with self.assertRaises(AttributeError):
            permission_func(mock_request, None, self.project)


class EdgeCasePermissionTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.user = factories.UserFactory()

    def test_permission_check_with_inactive_user(self):
        """Test permission checking with inactive user."""
        self.user.is_active = False
        self.user.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.customer.add_user(self.user, CustomerRole.OWNER)

        # Inactive users should not have any permissions
        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertFalse(result)  # Inactive user should not have permissions

    def test_permission_check_with_deleted_role(self):
        """Test permission checking when role is deleted."""
        role = models.Role.objects.create(
            name="TEMP.ROLE",
            content_type=ContentType.objects.get_for_model(self.customer),
        )
        role.add_permission(PermissionEnum.UPDATE_OFFERING)

        self.customer.add_user(self.user, role)

        # Verify permission works initially
        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

        # Delete the role
        role.delete()

        # Permission should be denied after role deletion
        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertFalse(result)

    def test_permission_check_with_role_permission_removed(self):
        """Test permission checking when permission is removed from role."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.customer.add_user(self.user, CustomerRole.OWNER)

        # Verify permission works initially
        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

        # Remove permission from role
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_OFFERING)

        # Permission should be denied after removal
        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertFalse(result)

    def test_circular_scope_reference_handling(self):
        """Test handling of circular references in scope resolution."""
        # Create mock with circular reference
        mock_scope = Mock()
        mock_scope.circular_ref = mock_scope

        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["circular_ref.circular_ref.customer"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should raise AttributeError when customer attribute doesn't exist on circular ref
        # Or TypeError when Django tries to process the mock object
        with self.assertRaises((AttributeError, TypeError)):
            permission_func(mock_request, None, mock_scope)

    def test_none_scope_in_source_path(self):
        """Test handling of None values in source path resolution."""
        mock_scope = Mock()
        mock_scope.project = None

        permission_func = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["project.customer"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should raise AttributeError when trying to access customer on None
        with self.assertRaises(AttributeError):
            permission_func(mock_request, None, mock_scope)


class PerformancePermissionTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer

    def test_permission_check_with_many_roles(self):
        """Test permission checking performance with many roles."""
        user = factories.UserFactory()

        # Create many roles for the user
        for i in range(10):
            role = models.Role.objects.create(
                name=f"TEST.ROLE.{i}",
                content_type=ContentType.objects.get_for_model(self.customer),
            )
            self.customer.add_user(user, role)

        # Add permission to last role only
        last_role = models.Role.objects.get(name="TEST.ROLE.9")
        last_role.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Permission check should still work efficiently
        result = utils.has_permission(
            user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

    def test_get_scope_ids_with_many_scopes(self):
        """Test get_scope_ids performance with many scopes."""
        user = factories.UserFactory()
        customers = []

        # Create many customers with user roles
        for i in range(5):
            customer = factories.CustomerFactory()
            customer.add_user(user, CustomerRole.OWNER)
            customers.append(customer)

        customer_ct = ContentType.objects.get_for_model(self.customer)
        scope_ids = list(utils.get_scope_ids(user, customer_ct))

        # Should return all customer IDs
        expected_ids = [c.id for c in customers]
        for customer_id in expected_ids:
            self.assertIn(customer_id, scope_ids)

    def test_has_permission_query_optimization(self):
        """Test that has_permission uses optimized queries."""
        user = factories.UserFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.customer.add_user(user, CustomerRole.OWNER)

        # Use connection.queries to verify query count if needed
        from django.test.utils import override_settings

        with override_settings(DEBUG=True):
            from django.db import connection

            initial_queries = len(connection.queries)
            utils.has_permission(user, PermissionEnum.UPDATE_OFFERING, self.customer)
            final_queries = len(connection.queries)

            # Should use minimal queries (typically 2: one for roles, one for permissions)
            query_count = final_queries - initial_queries
            self.assertLessEqual(query_count, 3)


class RolePermissionCRUDTest(TestCase):
    def setUp(self):
        self.customer_ct = ContentType.objects.get_by_natural_key(
            "structure", "customer"
        )
        self.role = models.Role.objects.create(
            name="TEST.ROLE", content_type=self.customer_ct
        )

    def test_add_permission_to_role(self):
        """Test adding permission to role."""
        self.role.add_permission(PermissionEnum.UPDATE_OFFERING)

        permission_exists = models.RolePermission.objects.filter(
            role=self.role, permission=PermissionEnum.UPDATE_OFFERING
        ).exists()
        self.assertTrue(permission_exists)

    def test_add_duplicate_permission_idempotent(self):
        """Test that adding duplicate permission is idempotent."""
        self.role.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.role.add_permission(PermissionEnum.UPDATE_OFFERING)  # Duplicate

        permission_count = models.RolePermission.objects.filter(
            role=self.role, permission=PermissionEnum.UPDATE_OFFERING
        ).count()
        self.assertEqual(permission_count, 1)

    def test_delete_permission_from_role(self):
        """Test deleting permission from role."""
        self.role.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.role.delete_permission(PermissionEnum.UPDATE_OFFERING)

        permission_exists = models.RolePermission.objects.filter(
            role=self.role, permission=PermissionEnum.UPDATE_OFFERING
        ).exists()
        self.assertFalse(permission_exists)

    def test_delete_nonexistent_permission_safe(self):
        """Test that deleting non-existent permission is safe."""
        # Should not raise exception
        self.role.delete_permission(PermissionEnum.CREATE_OFFERING)

    def test_role_permissions_cascade_delete(self):
        """Test that role permissions are deleted when role is deleted."""
        self.role.add_permission(PermissionEnum.UPDATE_OFFERING)
        role_id = self.role.id

        self.role.delete()

        # Permissions should be deleted too
        permission_exists = models.RolePermission.objects.filter(
            role_id=role_id
        ).exists()
        self.assertFalse(permission_exists)


class ScopeResolutionTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_get_customer_from_customer(self):
        """Test get_customer utility with customer object."""
        result = utils.get_customer(self.fixture.customer)
        self.assertEqual(result, self.fixture.customer)

    def test_get_customer_from_project(self):
        """Test get_customer utility with project object."""
        result = utils.get_customer(self.fixture.project)
        self.assertEqual(result, self.fixture.customer)

    def test_get_customer_with_mock_object(self):
        """Test get_customer with mock object having customer attribute."""
        mock_obj = Mock()
        mock_obj._meta.model_name = "resource"
        mock_obj.customer = self.fixture.customer

        result = utils.get_customer(mock_obj)
        self.assertEqual(result, self.fixture.customer)
