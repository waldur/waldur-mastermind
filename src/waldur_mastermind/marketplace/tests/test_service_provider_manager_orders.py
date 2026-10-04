"""A service provider manager holds their role on the ServiceProvider. Orders of
the provider's offerings are theirs to list and review provider-side, but the
grant must never reach the consumer side of an order, even when the provider
organization's own projects placed it."""

from pathlib import Path

import yaml
from ddt import data, ddt
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import tasks
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import ServiceProvider
from waldur_mastermind.marketplace.tests import factories, fixtures

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"

PROVIDER_ORDER_PERMISSIONS = (
    PermissionEnum.LIST_ORDERS,
    PermissionEnum.APPROVE_ORDER,
    PermissionEnum.REJECT_ORDER,
)


class ServiceProviderManagerOrderPermissionsTest(test.APITestCase):
    def test_shipped_role_grants_provider_order_review_only(self):
        roles = yaml.safe_load(PERMISSIONS_YAML.read_text())
        manager = next(
            role
            for role in roles
            if role["role"] == RoleEnum.CUSTOMER_MANAGER
            and role["scope"] == "service_provider"
        )
        granted = set(manager["permissions"])

        for permission in PROVIDER_ORDER_PERMISSIONS:
            self.assertIn(permission.value, granted)
        for permission in (
            PermissionEnum.APPROVE_PRIVATE_ORDER,
            PermissionEnum.CREATE_ORDER,
            PermissionEnum.CANCEL_ORDER,
            PermissionEnum.DESTROY_ORDER,
            PermissionEnum.SET_CONSUMER_ORDER_INFO,
        ):
            self.assertNotIn(permission.value, granted)


@ddt
class ServiceProviderManagerOrdersTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.provider_customer = self.service_provider.customer
        for permission in PROVIDER_ORDER_PERMISSIONS:
            ServiceProviderRole.MANAGER.add_permission(permission)

        self.manager = structure_factories.UserFactory()
        self.service_provider.add_user(self.manager, ServiceProviderRole.MANAGER)

        self.offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING, customer=self.provider_customer
        )
        self.order = self.make_order(self.offering, self.fixture.project)

        # Same role, different provider.
        other_provider = factories.ServiceProviderFactory()
        self.other_order = self.make_order(
            factories.OfferingFactory(
                type=SITE_AGENT_OFFERING, customer=other_provider.customer
            ),
            self.fixture.project,
        )

    def make_order(self, offering, project, state=OrderStates.PENDING_PROVIDER):
        return factories.OrderFactory(
            project=project,
            created_by=self.fixture.manager,
            offering=offering,
            resource=factories.ResourceFactory(offering=offering, project=project),
            state=state,
        )

    def list_orders(self, user=None, **params):
        self.client.force_authenticate(user or self.manager)
        response = self.client.get(factories.OrderFactory.get_list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {order["uuid"] for order in response.json()}

    def post(self, order, action, user=None, payload=None):
        self.client.force_authenticate(user or self.manager)
        response = self.client.post(
            factories.OrderFactory.get_url(order, action), payload
        )
        order.refresh_from_db()
        return response

    def test_manager_lists_orders_of_own_provider_only(self):
        self.assertEqual(
            self.list_orders(),
            {self.order.uuid.hex, self.fixture.order.uuid.hex},
        )

    def test_manager_does_not_list_orders_placed_by_provider_organization(self):
        """The provider organization's own project ordering elsewhere is the
        consumer side, which a ServiceProvider grant does not reach."""
        own_project = structure_factories.ProjectFactory(
            customer=self.provider_customer
        )
        consumer_order = self.make_order(
            self.other_order.offering, own_project, OrderStates.PENDING_CONSUMER
        )

        self.assertNotIn(consumer_order.uuid.hex, self.list_orders())

    def test_manager_gets_pending_provider_badge(self):
        self.assertEqual(
            self.list_orders(can_approve_as_provider=True), {self.order.uuid.hex}
        )

    def test_manager_can_approve_order(self):
        response = self.post(self.order, "approve_by_provider")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.state, OrderStates.EXECUTING)

    def test_manager_can_reject_order(self):
        response = self.post(self.order, "reject_by_provider")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)

    @data(
        ("set_state_executing", OrderStates.PENDING_PROVIDER, OrderStates.EXECUTING),
        ("set_state_done", OrderStates.EXECUTING, OrderStates.DONE),
        ("retry", OrderStates.ERRED, OrderStates.EXECUTING),
    )
    def test_manager_can_drive_order_state(self, case):
        action, initial_state, expected_state = case
        self.order.state = initial_state
        self.order.save()
        if action == "retry":
            self.order.resource.state = ResourceStates.ERRED
            self.order.resource.save()

        response = self.post(self.order, action)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.state, expected_state)

    def test_manager_can_set_provider_info(self):
        self.offering.plugin_options = {"enable_provider_consumer_messaging": True}
        self.offering.save()

        response = self.post(
            self.order, "set_provider_info", payload={"provider_message": "hi"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.provider_message, "hi")

    def test_manager_can_set_backend_id(self):
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.SET_RESOURCE_BACKEND_ID
        )

        response = self.post(
            self.order, "set_backend_id", payload={"backend_id": "job-42"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.backend_id, "job-42")

    @data("approve_by_provider", "reject_by_provider")
    def test_manager_can_not_review_order_of_another_provider(self, action):
        response = self.post(self.other_order, action)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.other_order.state, OrderStates.PENDING_PROVIDER)

    def test_manager_receives_pending_approval_notification(self):
        structure_factories.NotificationFactory(
            key="marketplace.notify_provider_about_pending_order"
        )

        tasks.notify_provider_about_pending_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.manager.email])

    @data("approve_by_consumer", "reject_by_consumer", "cancel", "set_consumer_info")
    def test_manager_gets_no_consumer_action(self, action):
        """An order the provider organization's own project placed for one of
        its offerings is visible provider-side, but its consumer side belongs
        to the project's roles."""
        for permission in (
            PermissionEnum.APPROVE_ORDER,
            PermissionEnum.REJECT_ORDER,
            PermissionEnum.CANCEL_ORDER,
            PermissionEnum.SET_CONSUMER_ORDER_INFO,
        ):
            ServiceProviderRole.MANAGER.add_permission(permission)
        own_project = structure_factories.ProjectFactory(
            customer=self.provider_customer
        )
        order = self.make_order(
            self.offering, own_project, OrderStates.PENDING_CONSUMER
        )
        self.assertIn(order.uuid.hex, self.list_orders())

        response = self.post(order, action, payload={"consumer_message": "hi"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(order.state, OrderStates.PENDING_CONSUMER)

    def test_manager_with_consumer_role_keeps_consumer_approval(self):
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_ORDERS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.fixture.project.add_user(self.manager, ProjectRole.ADMIN)
        order = self.make_order(
            self.offering, self.fixture.project, OrderStates.PENDING_CONSUMER
        )

        response = self.post(order, "approve_by_consumer")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_custom_role_with_list_only_can_not_approve(self):
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(ServiceProvider)
        )
        role.add_permission(PermissionEnum.LIST_ORDERS)
        user = structure_factories.UserFactory()
        self.service_provider.add_user(user, role)

        self.assertIn(self.order.uuid.hex, self.list_orders(user))
        self.assertEqual(self.list_orders(user, can_approve_as_provider=True), set())

        response = self.post(self.order, "approve_by_provider", user)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.order.state, OrderStates.PENDING_PROVIDER)

    def test_organization_owner_keeps_provider_review(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        owner = self.fixture.offering_owner

        self.assertIn(self.order.uuid.hex, self.list_orders(owner))
        response = self.post(self.order, "approve_by_provider", owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_offering_manager_keeps_provider_review(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        OfferingRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        user = structure_factories.UserFactory()
        self.offering.add_user(user, OfferingRole.MANAGER)

        self.assertEqual(self.list_orders(user), {self.order.uuid.hex})
        response = self.post(self.order, "approve_by_provider", user)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_organization_without_service_provider(self):
        """An offering of an organization that never registered as a provider
        must not break the provider-side checks."""
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        customer = structure_factories.CustomerFactory()
        owner = structure_factories.UserFactory()
        customer.add_user(owner, CustomerRole.OWNER)
        order = self.make_order(
            factories.OfferingFactory(type=SITE_AGENT_OFFERING, customer=customer),
            self.fixture.project,
        )

        response = self.post(order, "approve_by_provider", owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
