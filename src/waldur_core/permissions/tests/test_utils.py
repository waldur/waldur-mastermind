from unittest.mock import Mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework import exceptions, test

from waldur_core.permissions import models, utils
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories, fixtures


class HasPermissionUtilTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = self.fixture.owner
        self.staff_user = factories.UserFactory(is_staff=True)

    def test_staff_user_always_has_permission(self):
        """Staff users should always have permission regardless of role assignments."""
        result = utils.has_permission(
            self.staff_user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

    def test_user_with_permission_through_role(self):
        """User with proper role and permission should have access."""
        # Add permission to customer owner role
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertTrue(result)

    def test_user_without_permission_denied(self):
        """User without proper permission should be denied."""
        result = utils.has_permission(
            self.user, PermissionEnum.CREATE_OFFERING, self.customer
        )
        self.assertFalse(result)

    def test_user_without_role_in_scope_denied(self):
        """User without role in the specific scope should be denied."""
        other_customer = factories.CustomerFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, other_customer
        )
        self.assertFalse(result)

    def test_inactive_user_role_denied(self):
        """User with inactive role should be denied."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Deactivate the user role
        user_role = models.UserRole.objects.get(
            user=self.user, scope=self.customer, is_active=True
        )
        user_role.is_active = False
        user_role.save()

        result = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )
        self.assertFalse(result)

    def test_accepts_request_object_or_user(self):
        """Function should accept both request object and user directly."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Test with user directly
        result_user = utils.has_permission(
            self.user, PermissionEnum.UPDATE_OFFERING, self.customer
        )

        # Test with request object
        mock_request = Mock()
        mock_request.user = self.user
        result_request = utils.has_permission(
            mock_request, PermissionEnum.UPDATE_OFFERING, self.customer
        )

        self.assertTrue(result_user)
        self.assertEqual(result_user, result_request)


class PermissionFactoryTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = self.fixture.owner
        self.staff_user = factories.UserFactory(is_staff=True)

    def test_permission_factory_basic_functionality(self):
        """Test basic permission factory without sources."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        permission_function = utils.permission_factory(PermissionEnum.UPDATE_OFFERING)

        # Should not raise exception for user with permission
        mock_request = Mock()
        mock_request.user = self.user
        permission_function(mock_request, None, self.customer)

    def test_permission_factory_raises_exception_when_denied(self):
        """Test permission factory raises PermissionDenied when access is denied."""
        permission_function = utils.permission_factory(PermissionEnum.CREATE_OFFERING)

        mock_request = Mock()
        mock_request.user = self.user

        with self.assertRaises(exceptions.PermissionDenied):
            permission_function(mock_request, None, self.customer)

    def test_permission_factory_with_sources(self):
        """Test permission factory with source path resolution."""
        # Add permission to customer role since we're checking customer scope
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        permission_function = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["customer"]
        )

        mock_request = Mock()
        mock_request.user = self.fixture.owner  # Use customer owner, not project admin

        # Should check permission on project.customer
        permission_function(mock_request, None, self.project)

    def test_permission_factory_with_nested_sources(self):
        """Test permission factory with nested source paths."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Create a mock object with nested attributes
        mock_scope = Mock()
        mock_scope.project.customer = self.customer

        permission_function = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["project.customer"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should check permission on nested path
        permission_function(mock_request, None, mock_scope)

    def test_permission_factory_with_wildcard_source(self):
        """Test permission factory with wildcard source (current scope)."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        permission_function = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["*"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should check permission on current scope
        permission_function(mock_request, None, self.customer)

    def test_permission_factory_returns_early_without_scope(self):
        """Test permission factory returns early when no scope provided."""
        permission_function = utils.permission_factory(PermissionEnum.UPDATE_OFFERING)

        mock_request = Mock()
        mock_request.user = self.user

        # Should return without raising exception when scope is None
        result = permission_function(mock_request, None, None)
        self.assertIsNone(result)

    def test_permission_factory_metadata_attached(self):
        """Test that permission metadata is attached to the function."""
        sources = ["customer", "project"]
        permission_function = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, sources
        )

        self.assertEqual(permission_function.permission, PermissionEnum.UPDATE_OFFERING)
        self.assertEqual(permission_function.sources, sources)

    def test_permission_factory_staff_user_always_passes(self):
        """Test that staff users always pass permission checks."""
        permission_function = utils.permission_factory(PermissionEnum.CREATE_OFFERING)

        mock_request = Mock()
        mock_request.user = self.staff_user

        # Should not raise exception for staff user
        permission_function(mock_request, None, self.customer)

    def test_permission_factory_multiple_sources_any_match(self):
        """Test that permission factory succeeds if any source grants permission."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        # Create objects where user has permission only through one path
        mock_scope = Mock()
        mock_scope.other_customer = factories.CustomerFactory()  # No permission here
        mock_scope.customer = self.customer  # Permission here

        permission_function = utils.permission_factory(
            PermissionEnum.UPDATE_OFFERING, ["other_customer", "customer"]
        )

        mock_request = Mock()
        mock_request.user = self.user

        # Should succeed because user has permission through 'customer' path
        permission_function(mock_request, None, mock_scope)


class UtilityFunctionsTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = self.fixture.owner

    def test_get_users_with_role_filter(self):
        """Test getting users with specific role."""
        users = utils.get_users(self.customer, RoleEnum.CUSTOMER_OWNER)
        self.assertIn(self.user, users)

    def test_get_users_without_role_filter(self):
        """Test getting all users in scope."""
        users = utils.get_users(self.customer)
        self.assertIn(self.user, users)

    def test_get_users_with_permission(self):
        """Test getting users with specific permission."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        users = utils.get_users_with_permission(
            self.customer, PermissionEnum.UPDATE_OFFERING
        )
        self.assertIn(self.user, users)

    def test_get_scope_ids(self):
        """Test getting scope IDs for user with content type."""
        customer_ct = ContentType.objects.get_for_model(self.customer)
        scope_ids = utils.get_scope_ids(self.user, customer_ct)
        self.assertIn(self.customer.id, scope_ids)

    def test_get_scope_ids_with_role_filter(self):
        """Test getting scope IDs filtered by role."""
        customer_ct = ContentType.objects.get_for_model(self.customer)
        scope_ids = utils.get_scope_ids(
            self.user, customer_ct, role=RoleEnum.CUSTOMER_OWNER
        )
        self.assertIn(self.customer.id, scope_ids)

    def test_get_scope_ids_with_permission_filter(self):
        """Test getting scope IDs filtered by permission."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        customer_ct = ContentType.objects.get_for_model(self.customer)

        scope_ids = utils.get_scope_ids(
            self.user, customer_ct, permission=PermissionEnum.UPDATE_OFFERING
        )
        self.assertIn(self.customer.id, scope_ids)

    def test_count_users(self):
        """Test counting users in scope."""
        count = utils.count_users(self.customer)
        self.assertEqual(count, 1)  # Only the owner

    def test_get_customer_from_customer(self):
        """Test getting customer from customer object."""
        result = utils.get_customer(self.customer)
        self.assertEqual(result, self.customer)

    def test_get_customer_from_project(self):
        """Test getting customer from project object."""
        result = utils.get_customer(self.project)
        self.assertEqual(result, self.customer)


class UserManagementUtilsTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.user = factories.UserFactory()
        self.role = CustomerRole.SUPPORT

    def test_add_user(self):
        """Test adding user to scope with role."""
        user_role = utils.add_user(
            self.customer, self.user, self.role, created_by=self.fixture.owner
        )

        self.assertEqual(user_role.user, self.user)
        self.assertEqual(user_role.role, self.role)
        self.assertEqual(user_role.scope, self.customer)
        self.assertEqual(user_role.created_by, self.fixture.owner)

    def test_update_user_expiration(self):
        """Test updating user role expiration time."""
        from django.utils import timezone

        # First add user
        utils.add_user(self.customer, self.user, self.role)

        # Then update expiration
        expiration_time = timezone.now() + timezone.timedelta(days=30)
        result = utils.update_user(
            self.customer, self.user, self.role, expiration_time=expiration_time
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.expiration_time, expiration_time)

    def test_update_nonexistent_user_role(self):
        """Test updating non-existent user role returns False."""
        from django.utils import timezone

        expiration_time = timezone.now() + timezone.timedelta(days=30)
        result = utils.update_user(
            self.customer, self.user, self.role, expiration_time=expiration_time
        )

        self.assertFalse(result)

    def test_delete_user(self):
        """Test deleting user role."""
        # First add user
        utils.add_user(self.customer, self.user, self.role)

        # Then delete
        result = utils.delete_user(self.customer, self.user, self.role)

        self.assertTrue(result)

        # Verify role is inactive
        user_role = models.UserRole.objects.get(
            user=self.user, role=self.role, scope=self.customer
        )
        self.assertFalse(user_role.is_active)

    def test_delete_nonexistent_user_role(self):
        """Test deleting non-existent user role returns False."""
        result = utils.delete_user(self.customer, self.user, self.role)
        self.assertFalse(result)


class HasUserUtilTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.user = factories.UserFactory()
        self.role = CustomerRole.SUPPORT

    def test_has_user_basic(self):
        """Test basic has_user functionality."""
        utils.add_user(self.customer, self.user, self.role)

        result = utils.has_user(self.customer, self.user, self.role)
        self.assertTrue(result)

    def test_has_user_without_role_filter(self):
        """Test has_user without specific role."""
        utils.add_user(self.customer, self.user, self.role)

        result = utils.has_user(self.customer, self.user)
        self.assertTrue(result)

    def test_has_user_with_permanent_role_filter(self):
        """Test has_user checking for permanent roles only."""
        utils.add_user(self.customer, self.user, self.role)

        result = utils.has_user(self.customer, self.user, expiration_time=None)
        self.assertTrue(result)

    def test_has_user_with_time_based_role(self):
        """Test has_user with time-based role checking."""
        from django.utils import timezone

        expiration_time = timezone.now() + timezone.timedelta(days=30)
        utils.add_user(
            self.customer, self.user, self.role, expiration_time=expiration_time
        )

        # Check if user will have role in 15 days
        future_time = timezone.now() + timezone.timedelta(days=15)
        result = utils.has_user(self.customer, self.user, expiration_time=future_time)
        self.assertTrue(result)

    def test_has_user_role_expired(self):
        """Test has_user with expired role."""
        from django.utils import timezone

        past_expiration = timezone.now() - timezone.timedelta(days=1)
        utils.add_user(
            self.customer, self.user, self.role, expiration_time=past_expiration
        )

        # Check if user has role now (should be False due to expiration)
        current_time = timezone.now()
        result = utils.has_user(self.customer, self.user, expiration_time=current_time)
        self.assertFalse(result)

    def test_has_user_inactive_role(self):
        """Test has_user with inactive role."""
        user_role = utils.add_user(self.customer, self.user, self.role)
        user_role.is_active = False
        user_role.save()

        result = utils.has_user(self.customer, self.user, self.role)
        self.assertFalse(result)
