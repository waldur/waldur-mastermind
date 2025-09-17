"""
Test to reproduce permission issue where PROJECT.MANAGER user gets
"No Instance matches the given query" error when terminating
Marketplace.Resource with connected OpenStack Instance scope.

The issue is that PROJECT.MANAGER users get a "No Resource matches the given query"
error when terminating marketplace resources, despite having the proper permissions.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OrderTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_openstack.tests import factories as openstack_factories


class ProjectManagerTerminationPermissionBugTest(test.APITransactionTestCase):
    """
    Test to reproduce the production issue where PROJECT.MANAGER users
    cannot terminate marketplace resources with OpenStack Instance scopes.
    """

    def setUp(self):
        # Set up basic marketplace structure
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer

        # Create a PROJECT.MANAGER user
        self.project_manager = structure_factories.UserFactory()
        self.project.add_user(self.project_manager, RoleEnum.PROJECT_MANAGER)

        # Create a PROJECT.ADMIN user
        self.project_admin = structure_factories.UserFactory()
        self.project.add_user(self.project_admin, RoleEnum.PROJECT_ADMIN)

        # Create a staff user for comparison
        self.staff_user = structure_factories.UserFactory(is_staff=True)

        # Set up OpenStack service settings and tenant
        self.service_settings = openstack_factories.SettingsFactory()
        self.tenant = openstack_factories.TenantFactory(
            service_settings=self.service_settings, project=self.project
        )

        # Create OpenStack Instance
        self.instance = openstack_factories.InstanceFactory(
            tenant=self.tenant, project=self.project, state=CoreStates.OK
        )

        # Create marketplace offering for OpenStack instances
        self.offering = factories.OfferingFactory(
            scope=self.service_settings, type="OpenStackTenant.Instance"
        )
        self.plan = factories.PlanFactory(offering=self.offering)

        # Create marketplace resource linked to the OpenStack instance
        self.marketplace_resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            scope=self.instance,  # This is the key: scope points to OpenStack Instance
            state=ResourceStates.OK,
        )

        # Grant TERMINATE_RESOURCE permission to both roles
        # (this is typically set in production)
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.TERMINATE_RESOURCE)

        # The key missing permission: LIST_RESOURCES is needed for filter_for_service_consumer
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_RESOURCES)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_RESOURCES)

        self.termination_url = factories.ResourceFactory.get_url(
            self.marketplace_resource, "terminate"
        )

    def test_staff_user_can_terminate_marketplace_resource_with_openstack_scope(self):
        """
        Verify that staff users can successfully terminate the resource.
        This should work as expected.
        """
        self.client.force_authenticate(self.staff_user)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify order was created
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.type, OrderTypes.TERMINATE)
        self.assertEqual(order.resource, self.marketplace_resource)

    def test_project_manager_termination_with_fix(self):
        """
        Test that PROJECT.MANAGER user can now terminate marketplace resources
        after the fix to DeleteScopedResourceProcessor.validate_order().

        The fix adds proper request context to the viewset validation,
        allowing PROJECT.MANAGER users to access OpenStack instances through
        the proper permission chain.
        """
        self.client.force_authenticate(self.project_manager)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        # Verify successful termination order creation
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("order_uuid", response.data)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.type, OrderTypes.TERMINATE)
        self.assertEqual(order.resource, self.marketplace_resource)
        self.assertEqual(order.created_by, self.project_manager)

    def test_project_admin_can_terminate_marketplace_resource_with_openstack_scope(
        self,
    ):
        """
        Test that PROJECT.ADMIN user can terminate marketplace resources
        with OpenStack Instance scopes. This verifies the fix works for
        both manager and admin roles.
        """
        self.client.force_authenticate(self.project_admin)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        # Verify successful termination order creation
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("order_uuid", response.data)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.type, OrderTypes.TERMINATE)
        self.assertEqual(order.resource, self.marketplace_resource)
        self.assertEqual(order.created_by, self.project_admin)

    def test_unauthorized_user_cannot_terminate_marketplace_resource(self):
        """
        Test that users without proper permissions cannot terminate
        marketplace resources, ensuring the fix doesn't break security.
        """
        # Create an unauthorized user (not project member)
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        # Should get permission denied
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_member_without_permissions_cannot_terminate(self):
        """
        Test that project members without TERMINATE_RESOURCE permission
        cannot terminate resources.
        """
        # Create a project member without termination permissions
        project_member = structure_factories.UserFactory()
        # Add as basic member (not manager or admin)
        self.project.add_user(project_member, RoleEnum.PROJECT_MEMBER)

        self.client.force_authenticate(project_member)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        # Should get permission denied
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
