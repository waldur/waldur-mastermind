from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class CreateComponentUserUsageLimitTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )
        ProjectRole.MANAGER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )

    @data("staff", "owner", "manager")
    def test_user_can_create_limit(self, user):
        response = self.create_component_user_usage_limit(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.ComponentUserUsageLimit.objects.filter(
                component=self.fixture.offering_component
            ).exists()
        )

    @data("admin", "user")
    def test_user_can_not_create_limit(self, user):
        response = self.create_component_user_usage_limit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_component_user_usage_limit(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ComponentUserUsageLimitFactory.get_list_url()
        offering_user = factories.OfferingUserFactory(offering=self.offering)
        payload = {
            "resource": factories.ResourceFactory.get_url(self.fixture.resource),
            "component": self.fixture.offering_component.uuid.hex,
            "user": factories.OfferingUserFactory.get_url(offering_user),
            "limit": 100,
        }
        return self.client.post(url, payload)


@ddt
class GetComponentUserUsageLimitTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.component_user_usage_limit = factories.ComponentUserUsageLimitFactory(
            resource=self.fixture.resource,
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )
        ProjectRole.MANAGER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )

    @data("staff", "owner", "manager", "admin")
    def test_user_can_get_limits(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ComponentUserUsageLimitFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)

    @data("user")
    def test_user_can_not_get_limits(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ComponentUserUsageLimitFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(len(response.data), 0)


@ddt
class UpdateComponentUserUsageLimitTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.component_user_usage_limit = factories.ComponentUserUsageLimitFactory(
            resource=self.fixture.resource,
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )
        ProjectRole.MANAGER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )

    @data("staff", "owner", "manager")
    def test_user_can_update_limit(self, user):
        response = self.update_component_user_usage_limit(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("admin")
    def test_user_can_not_update_limit(self, user):
        response = self.update_component_user_usage_limit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_component_user_usage_limit(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ComponentUserUsageLimitFactory.get_url(
            self.component_user_usage_limit
        )
        payload = {
            "limit": 50,
        }
        return self.client.patch(url, payload)


@ddt
class DeleteComponentUserUsageLimitTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.component_user_usage_limit = factories.ComponentUserUsageLimitFactory(
            resource=self.fixture.resource,
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )
        ProjectRole.MANAGER.add_permission(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION
        )

    @data("staff", "owner", "manager")
    def test_user_can_delete_limit(self, user):
        response = self.delete_component_user_usage_limit(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("admin")
    def test_user_can_not_delete_limit(self, user):
        response = self.delete_component_user_usage_limit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def delete_component_user_usage_limit(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ComponentUserUsageLimitFactory.get_url(
            self.component_user_usage_limit
        )
        return self.client.delete(url)
