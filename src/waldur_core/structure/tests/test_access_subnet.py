from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories, fixtures


class AccessSubnetIpFilterTest(test.APITestCase):
    """The customer-visibility IP filter must not crash on a non-IP header."""

    def setUp(self):
        self.fixture = fixtures.CustomerFixture()

    def test_non_ip_forwarded_header_does_not_500(self):
        # filter_queryset_by_user_ip feeds the resolved client IP into a
        # Postgres inet lookup. A non-IP X-Forwarded-For must normalise to None
        # (which takes the existing "no IP -> no restriction" path) instead of
        # crashing query construction with a 500.
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get("/api/customers/", HTTP_X_FORWARDED_FOR="not-an-ip")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class AccessSubnetCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer_url = factories.CustomerFactory.get_url(
            customer=self.fixture.customer
        )
        self.project_user = self.fixture.user
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ACCESS_SUBNET)

    @data("staff", "owner")
    def test_user_can_create_single_host_access_subnet(self, user):
        user = getattr(self.fixture, user)
        response = self.create_access_subnet(user, inet="192.168.1.5/32")
        self.assertEqual(response.status_code, 201, response.data)

    def test_owner_cannot_create_wider_than_single_host(self):
        # The declared CharField used to drop the model's /32 validator, so the
        # API accepted any width. Non-staff are now held to a single host.
        response = self.create_access_subnet(self.fixture.owner)
        self.assertEqual(response.status_code, 400, response.data)

    def test_staff_can_create_wider_network(self):
        response = self.create_access_subnet(self.fixture.staff)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["is_staff_managed"])

    def test_staff_cannot_create_zero_prefix(self):
        response = self.create_access_subnet(self.fixture.staff, inet="0.0.0.0/0")
        self.assertEqual(response.status_code, 400, response.data)

    def test_project_user_cannot_create_access_subnet(self):
        # A single host, so the mask rule passes and the permission check is
        # what the response actually reflects.
        response = self.create_access_subnet(self.project_user, inet="192.168.1.5/32")
        self.assertEqual(response.status_code, 403)

    def create_access_subnet(self, user, inet="192.168.1.0/24"):
        self.client.force_authenticate(user=user)
        url = factories.AccessSubnetFactory.get_list_url()
        payload = {
            "customer": self.customer_url,
            "inet": inet,
            "description": "Test subnet",
        }
        response = self.client.post(
            url,
            payload,
        )
        return response


@ddt
class AccessSubnetUpdateTest(test.APITestCase):
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
class AccessSubnetDeleteTest(test.APITestCase):
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
class AccessSubnetGetTest(test.APITestCase):
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


class AccessSubnetOrderingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_access_subnet_ordering_by_inet(self):
        # Create access subnets with different inet values to test ordering
        subnet1 = factories.AccessSubnetFactory(
            customer=self.fixture.customer, inet="192.168.3.0/24"
        )
        subnet2 = factories.AccessSubnetFactory(
            customer=self.fixture.customer, inet="192.168.1.0/24"
        )
        subnet3 = factories.AccessSubnetFactory(
            customer=self.fixture.customer, inet="192.168.2.0/24"
        )

        # Test model ordering
        from waldur_core.structure.models import AccessSubnet

        subnets = list(AccessSubnet.objects.filter(customer=self.fixture.customer))

        # Should be ordered by inet field
        expected_order = [
            subnet2,
            subnet3,
            subnet1,
        ]  # 192.168.1.0, 192.168.2.0, 192.168.3.0
        self.assertEqual(subnets, expected_order)

        # Test API ordering
        self.client.force_authenticate(user=self.fixture.staff)
        url = factories.AccessSubnetFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Verify API response is ordered by inet
        inets = [item["inet"] for item in response.data]
        expected_inets = ["192.168.1.0/24", "192.168.2.0/24", "192.168.3.0/24"]
        self.assertEqual(inets, expected_inets)
