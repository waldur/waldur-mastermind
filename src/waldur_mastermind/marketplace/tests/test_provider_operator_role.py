"""A least-privilege, customer-scoped provider role.

Provider A and provider B each sell an offering to a consumer of their own. The
operator holds a custom role on provider A's organization only, with the
provider-side permissions of the support request and none of an owner's.
"""

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.serializers import clone_role_for_customer
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import fixtures

OPERATOR_PERMISSIONS = [
    PermissionEnum.LIST_ORDERS,
    PermissionEnum.APPROVE_ORDER,
    PermissionEnum.REJECT_ORDER,
    PermissionEnum.LIST_RESOURCES,
    PermissionEnum.UPDATE_RESOURCE_OPTIONS,
    PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS,
    PermissionEnum.GET_SERVICE_PROVIDER_REVENUE,
    PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS,
    PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMER_PROJECTS,
    PermissionEnum.LIST_SERVICE_PROVIDER_PROJECTS,
]


@freeze_time("2020-11-01")
class ProviderOperatorRoleTest(test.APITestCase):
    def setUp(self):
        self.provider_a = fixtures.MarketplaceFixture()
        self.provider_b = fixtures.MarketplaceFixture()
        for fixture in (self.provider_a, self.provider_b):
            fixture.resource.set_state_ok()
            fixture.resource.save()

        self.role = self.make_role(
            "CUSTOMER.SERVICE_PROVIDER_OPERATOR", OPERATOR_PERMISSIONS
        )
        self.operator = self.make_operator(self.role)

    def make_role(self, name, permissions):
        role = Role.objects.create(
            name=name,
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
        )
        for permission in permissions:
            role.add_permission(permission)
        return role

    def make_operator(self, role):
        user = structure_factories.UserFactory()
        self.provider_a.offering_customer.add_user(user, role)
        return user

    def get(self, url, user=None):
        self.client.force_authenticate(user or self.operator)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_operator_sees_invoice_items_of_own_offerings_only(self):
        rows = self.get(reverse("provider-invoice-items-list"))
        self.assertEqual(
            {row["resource_uuid"] for row in rows},
            {self.provider_a.resource.uuid.hex},
        )

    def test_invoice_items_need_the_revenue_permission(self):
        role = self.make_role(
            "CUSTOMER.NO_REVENUE",
            [
                p
                for p in OPERATOR_PERMISSIONS
                if p != PermissionEnum.GET_SERVICE_PROVIDER_REVENUE
            ],
        )
        rows = self.get(
            reverse("provider-invoice-items-list"), self.make_operator(role)
        )
        self.assertEqual(rows, [])

    def test_clone_narrowed_by_staff_loses_the_revenue_permission(self):
        # Its template still holds GET_REVENUE; only the clone's own
        # permissions may count.
        clone = clone_role_for_customer(self.role, self.provider_a.offering_customer)
        clone.delete_permission(PermissionEnum.GET_SERVICE_PROVIDER_REVENUE)
        rows = self.get(
            reverse("provider-invoice-items-list"), self.make_operator(clone)
        )
        self.assertEqual(rows, [])

    def test_owner_and_service_provider_manager_keep_invoice_items(self):
        # permissions.yaml grants both roles GET_REVENUE; tests start bare.
        CustomerRole.OWNER.add_permission(PermissionEnum.GET_SERVICE_PROVIDER_REVENUE)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_REVENUE
        )
        owner = structure_factories.UserFactory()
        self.provider_a.offering_customer.add_user(owner, CustomerRole.OWNER)
        # In deployments the manager role is held on the ServiceProvider.
        manager = structure_factories.UserFactory()
        self.provider_a.service_provider.add_user(manager, ServiceProviderRole.MANAGER)

        for user in (owner, manager):
            rows = self.get(reverse("provider-invoice-items-list"), user)
            self.assertEqual(
                {row["resource_uuid"] for row in rows},
                {self.provider_a.resource.uuid.hex},
            )

    def test_operator_sees_consumers_users_with_list_users_permission(self):
        self.role.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)
        consumer_a = self.provider_a.admin
        consumer_b = self.provider_b.admin

        usernames = {
            row["username"]
            for row in self.get(structure_factories.UserFactory.get_list_url())
        }

        self.assertIn(consumer_a.username, usernames)
        self.assertNotIn(consumer_b.username, usernames)

    def test_consumers_users_need_the_list_users_permission(self):
        consumer_a = self.provider_a.admin

        usernames = {
            row["username"]
            for row in self.get(structure_factories.UserFactory.get_list_url())
        }

        self.assertNotIn(consumer_a.username, usernames)

    def test_operator_sees_only_customers_of_own_provider(self):
        url = reverse(
            "service-provider-customers-list",
            kwargs={"service_provider_uuid": self.provider_a.service_provider.uuid.hex},
        )
        rows = self.get(url)
        self.assertEqual(
            {row["uuid"] for row in rows}, {self.provider_a.customer.uuid.hex}
        )

    def test_operator_cannot_list_customers_of_another_provider(self):
        url = reverse(
            "service-provider-customers-list",
            kwargs={"service_provider_uuid": self.provider_b.service_provider.uuid.hex},
        )
        self.client.force_authenticate(self.operator)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_operator_sees_orders_of_own_offerings_only(self):
        rows = self.get(reverse("marketplace-order-list"))
        self.assertEqual(
            {row["uuid"] for row in rows}, {self.provider_a.order.uuid.hex}
        )

    def test_operator_can_approve_provider_order_of_own_offering(self):
        order = self.provider_a.order
        order.state = OrderStates.PENDING_PROVIDER
        order.save(update_fields=["state"])
        self.client.force_authenticate(self.operator)
        url = reverse(
            "marketplace-order-approve-by-provider",
            kwargs={"uuid": order.uuid.hex},
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_operator_cannot_approve_order_of_another_provider(self):
        order = self.provider_b.order
        order.state = OrderStates.PENDING_PROVIDER
        order.save(update_fields=["state"])
        self.client.force_authenticate(self.operator)
        url = reverse(
            "marketplace-order-approve-by-provider",
            kwargs={"uuid": order.uuid.hex},
        )
        response = self.client.post(url)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_operator_sees_resources_of_own_offerings_only(self):
        rows = self.get(reverse("marketplace-provider-resource-list"))
        self.assertEqual(
            {row["uuid"] for row in rows}, {self.provider_a.resource.uuid.hex}
        )

    def test_operator_can_reject_provider_order_of_own_offering(self):
        order = self.provider_a.order
        order.state = OrderStates.PENDING_PROVIDER
        order.save(update_fields=["state"])
        self.client.force_authenticate(self.operator)
        url = reverse(
            "marketplace-order-reject-by-provider",
            kwargs={"uuid": order.uuid.hex},
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.REJECTED)

    def test_operator_cannot_create_offering(self):
        self.client.force_authenticate(self.operator)
        response = self.client.post(
            reverse("marketplace-provider-offering-list"),
            {
                "name": "Operator offering",
                "category": reverse(
                    "marketplace-category-detail",
                    kwargs={"uuid": self.provider_a.offering.category.uuid.hex},
                ),
                "customer": structure_factories.CustomerFactory.get_url(
                    self.provider_a.offering_customer
                ),
                "type": self.provider_a.offering.type,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
