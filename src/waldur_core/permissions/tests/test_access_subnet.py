from unittest import mock

from rest_framework import test

from waldur_core.structure.models import AccessSubnet
from waldur_core.structure.tests import fixtures


class UserPermissionAccessSubnetFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.customer.save()

        self.access_subnet = AccessSubnet.objects.create(
            customer=self.customer, inet="128.0.0.0/16"
        )

        self.patcher = mock.patch("waldur_core.structure.managers.core_utils")
        self.mock = self.patcher.start()
        self.mock.get_ip_address.return_value = "127.0.0.1"

        self.url = "/api/user-permissions/"

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_user_can_get_user_permissions_only_if_his_ip_contains_inet(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

        self.customer = self.fixture.customer
        self.access_subnet.inet = "127.0.0.0/24"
        self.access_subnet.save()
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

        self.customer = self.fixture.customer
        self.access_subnet.inet = ""
        self.access_subnet.save()
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
