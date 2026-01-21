"""
Test to verify that PROJECT.MANAGER users can terminate marketplace
Volume resources without getting "No Volume matches the given query" error.

This is similar to the instance termination permission bug fix, but for volumes.
The fix involves using delete_volume utility directly instead of going through
the viewset with permission filtering.
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


class VolumeTerminationPermissionTest(test.APITransactionTestCase):
    """
    Test that PROJECT.MANAGER users can terminate marketplace resources
    with OpenStack Volume scopes without permission errors.
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

        # Create OpenStack Volume
        self.volume = openstack_factories.VolumeFactory(
            tenant=self.tenant,
            project=self.project,
            state=CoreStates.OK,
            runtime_state="available",
        )

        # Create marketplace offering for OpenStack volumes
        self.offering = factories.OfferingFactory(
            scope=self.tenant, type="OpenStack.Volume"
        )
        self.plan = factories.PlanFactory(offering=self.offering)

        # Create marketplace resource linked to the OpenStack volume
        self.marketplace_resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            scope=self.volume,
            state=ResourceStates.OK,
        )

        # Grant TERMINATE_RESOURCE permission to both roles
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.TERMINATE_RESOURCE)

        # Grant LIST_RESOURCES permission needed for filter_for_service_consumer
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_RESOURCES)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_RESOURCES)

        self.termination_url = factories.ResourceFactory.get_url(
            self.marketplace_resource, "terminate"
        )

    def test_staff_user_can_terminate_volume_marketplace_resource(self):
        """
        Verify that staff users can successfully terminate volume resources.
        """
        self.client.force_authenticate(self.staff_user)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify order was created
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.type, OrderTypes.TERMINATE)
        self.assertEqual(order.resource, self.marketplace_resource)

    def test_project_manager_can_terminate_volume_marketplace_resource(self):
        """
        Test that PROJECT.MANAGER user can terminate marketplace volume resources.

        This test verifies the fix where VolumeDeleteProcessor now uses
        delete_volume utility directly instead of going through the viewset
        with GenericRoleFilter permission filtering.
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

    def test_project_admin_can_terminate_volume_marketplace_resource(self):
        """
        Test that PROJECT.ADMIN user can terminate marketplace volume resources.
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

    def test_unauthorized_user_cannot_terminate_volume_marketplace_resource(self):
        """
        Test that users without proper permissions cannot terminate
        marketplace volume resources.
        """
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_member_without_permissions_cannot_terminate_volume(self):
        """
        Test that project members without TERMINATE_RESOURCE permission
        cannot terminate volume resources.
        """
        project_member = structure_factories.UserFactory()
        self.project.add_user(project_member, RoleEnum.PROJECT_MEMBER)

        self.client.force_authenticate(project_member)

        with mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay"):
            response = self.client.post(self.termination_url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
