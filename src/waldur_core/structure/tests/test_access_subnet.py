from unittest import mock

from ddt import data, ddt
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories, fixtures


@ddt
class AccessSubnetCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer_url = factories.CustomerFactory.get_url(
            customer=self.fixture.customer
        )
        self.project_user = self.fixture.user
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ACCESS_SUBNET)

    @data("staff", "owner")
    def test_user_can_create_access_subnet(self, user):
        user = getattr(self.fixture, user)
        response = self.create_access_subnet(user)
        self.assertEqual(response.status_code, 201)

    def test_project_user_cannot_create_access_subnet(self):
        response = self.create_access_subnet(self.project_user)
        self.assertEqual(response.status_code, 403)

    def create_access_subnet(self, user):
        self.client.force_authenticate(user=user)
        url = factories.AccessSubnetFactory.get_list_url()
        payload = {
            "customer": self.customer_url,
            "inet": "192.168.1.0/24",
            "description": "Test subnet",
        }
        response = self.client.post(
            url,
            payload,
        )
        return response


@ddt
class AccessSubnetUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.patcher = mock.patch("waldur_core.structure.managers.core_utils")
        self.mock = self.patcher.start()
        self.mock.get_ip_address.return_value = "143.176.2.2"
        self.access_subnet = factories.AccessSubnetFactory(
            customer=self.fixture.customer
        )
        self.project_user = self.fixture.user
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_ACCESS_SUBNET)

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    @data("staff", "owner")
    def test_user_can_update_access_subnet(self, user):
        user = getattr(self.fixture, user)
        new_description = "Updated subnet"
        response = self.update_access_subnet(user, self.access_subnet, new_description)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.data["description"],
            new_description,
        )

    def test_project_user_cannot_update_access_subnet(self):
        response = self.update_access_subnet(
            self.project_user, self.access_subnet, "Updated subnet"
        )
        self.assertEqual(response.status_code, 404)

    @data("service_manager")
    def test_service_manager_cannot_update_access_subnet(self, user):
        user = getattr(self.fixture, user)
        response = self.update_access_subnet(user, self.access_subnet, "Updated subnet")
        self.assertEqual(response.status_code, 403)

    def update_access_subnet(self, user, access_subnet, new_description):
        self.client.force_authenticate(user=user)
        url = factories.AccessSubnetFactory.get_url(access_subnet)
        payload = {
            "description": new_description,
        }
        response = self.client.patch(
            url,
            payload,
        )
        return response


@ddt
class AccessSubnetDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project_user = self.fixture.user
        self.access_subnet = factories.AccessSubnetFactory(
            customer=self.fixture.customer
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_ACCESS_SUBNET)

    @data("staff", "owner")
    def test_user_can_delete_access_subnet(self, user):
        user = getattr(self.fixture, user)
        response = self.delete_access_subnet(user, self.access_subnet)
        self.assertEqual(response.status_code, 204)

    def test_project_user_cannot_delete_access_subnet(self):
        response = self.delete_access_subnet(self.project_user, self.access_subnet)
        self.assertEqual(response.status_code, 404)

    @data("service_manager")
    def test_service_manager_cannot_delete_access_subnet(self, user):
        user = getattr(self.fixture, user)
        response = self.delete_access_subnet(user, self.access_subnet)
        self.assertEqual(response.status_code, 403)

    def delete_access_subnet(self, user, access_subnet):
        self.client.force_authenticate(user=user)
        url = factories.AccessSubnetFactory.get_url(access_subnet)
        response = self.client.delete(url)
        return response


@ddt
class AccessSubnetGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.access_subnet = factories.AccessSubnetFactory(
            customer=self.fixture.customer
        )
        self.url = factories.AccessSubnetFactory.get_list_url()

    def test_unauthenticated_user_cannot_get_access_subnet(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    @data("staff", "owner", "service_manager")
    def test_user_can_get_access_subnet(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_project_user_cannot_get_access_subnet(self):
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 0)
