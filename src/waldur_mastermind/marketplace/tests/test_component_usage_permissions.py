from ddt import data, ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests.factories import (
    CustomerFactory,
    ProjectFactory,
    UserFactory,
)
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


@ddt
class ComponentUsageListPermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.list_url = factories.ComponentUsageFactory.get_list_url()
        self.own_usage = factories.ComponentUsageFactory(
            resource=self.fixture.resource,
            component=self.fixture.offering_component,
        )
        self.other_usage = factories.ComponentUsageFactory()

        self.other_customer_owner = UserFactory()
        self.other_customer = CustomerFactory()
        self.other_project = ProjectFactory(customer=self.other_customer)
        self.other_customer.add_user(self.other_customer_owner, CustomerRole.OWNER)

        self.unrelated_user = UserFactory()

    def _get_usage_uuids(self, response):
        return {item["uuid"] for item in response.data}

    @data("owner", "admin", "manager", "provider_owner", "provider_manager")
    def test_connected_user_can_list_own_component_usages(self, user_attr):
        user = getattr(self.fixture, user_attr)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usage_uuids = self._get_usage_uuids(response)
        self.assertIn(self.own_usage.uuid.hex, usage_uuids)
        self.assertNotIn(self.other_usage.uuid.hex, usage_uuids)

    @data("staff")
    def test_staff_can_list_all_component_usages(self, user_attr):
        user = getattr(self.fixture, user_attr)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usage_uuids = self._get_usage_uuids(response)
        self.assertIn(self.own_usage.uuid.hex, usage_uuids)
        self.assertIn(self.other_usage.uuid.hex, usage_uuids)

    @data("user")
    def test_unrelated_user_cannot_list_component_usages(self, user_attr):
        user = getattr(self.fixture, user_attr)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_other_customer_owner_cannot_list_foreign_component_usages(self):
        self.client.force_authenticate(self.other_customer_owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_other_customer_owner_cannot_retrieve_foreign_component_usage(self):
        self.client.force_authenticate(self.other_customer_owner)
        url = reverse(
            "marketplace-component-usage-detail",
            kwargs={"uuid": self.own_usage.uuid.hex},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_can_retrieve_own_component_usage(self):
        self.client.force_authenticate(self.fixture.owner)
        url = reverse(
            "marketplace-component-usage-detail",
            kwargs={"uuid": self.own_usage.uuid.hex},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.own_usage.uuid.hex)


@ddt
class ComponentUserUsageListPermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.list_url = reverse("marketplace-component-user-usage-list")
        self.own_component_usage = factories.ComponentUsageFactory(
            resource=self.fixture.resource,
            component=self.fixture.offering_component,
        )
        self.other_component_usage = factories.ComponentUsageFactory()
        self.own_user_usage = models.ComponentUserUsage.objects.create(
            component_usage=self.own_component_usage,
            username="own-user",
            usage=10,
        )
        self.other_user_usage = models.ComponentUserUsage.objects.create(
            component_usage=self.other_component_usage,
            username="other-user",
            usage=20,
        )

        self.other_customer_owner = UserFactory()
        self.other_customer = CustomerFactory()
        self.other_project = ProjectFactory(customer=self.other_customer)
        self.other_customer.add_user(self.other_customer_owner, CustomerRole.OWNER)

        self.unrelated_user = UserFactory()

    def _get_usage_uuids(self, response):
        return {item["uuid"] for item in response.data}

    @data("owner", "admin", "manager", "provider_owner", "provider_manager")
    def test_connected_user_can_list_own_component_user_usages(self, user_attr):
        user = getattr(self.fixture, user_attr)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usage_uuids = self._get_usage_uuids(response)
        self.assertIn(self.own_user_usage.uuid.hex, usage_uuids)
        self.assertNotIn(self.other_user_usage.uuid.hex, usage_uuids)

    @data("staff")
    def test_staff_can_list_all_component_user_usages(self, user_attr):
        user = getattr(self.fixture, user_attr)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usage_uuids = self._get_usage_uuids(response)
        self.assertIn(self.own_user_usage.uuid.hex, usage_uuids)
        self.assertIn(self.other_user_usage.uuid.hex, usage_uuids)

    @data("user")
    def test_unrelated_user_cannot_list_component_user_usages(self, user_attr):
        user = getattr(self.fixture, user_attr)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_other_customer_owner_cannot_list_foreign_component_user_usages(self):
        self.client.force_authenticate(self.other_customer_owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_other_customer_owner_cannot_retrieve_foreign_component_user_usage(self):
        self.client.force_authenticate(self.other_customer_owner)
        url = reverse(
            "marketplace-component-user-usage-detail",
            kwargs={"uuid": self.own_user_usage.uuid.hex},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_can_retrieve_own_component_user_usage(self):
        self.client.force_authenticate(self.fixture.owner)
        url = reverse(
            "marketplace-component-user-usage-detail",
            kwargs={"uuid": self.own_user_usage.uuid.hex},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.own_user_usage.uuid.hex)
