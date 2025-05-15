from unittest import mock

from rest_framework import status, test

from . import factories, fixtures


class BaseRouterTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)


class SetRoutesTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.url = factories.RouterFactory.get_url(self.router, action="set_routes")

    def test_positive(self):
        response = self.client.post(
            self.url, {"routes": [{"destination": "1.1.1.1", "nexthop": "10.10.10.10"}]}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_negative(self):
        response = self.client.post(
            self.url,
            {"routes": [{"destination": "1.1.1.1/33", "nexthop": "10.10.10.10"}]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RouterInterfaceTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.subnet = factories.SubNetFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.url_add = factories.RouterFactory.get_url(
            self.router, action="add_router_interface"
        )
        self.url_remove = factories.RouterFactory.get_url(
            self.router, action="remove_router_interface"
        )

    @mock.patch("waldur_openstack.backend.OpenStackBackend.add_router_interface")
    def test_add_router_interface_with_subnet(self, mock_execute):
        response = self.client.post(
            self.url_add, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.backend.OpenStackBackend.add_router_interface")
    def test_add_router_interface_with_port(self, mock_execute):
        response = self.client.post(
            self.url_add, {"port": factories.PortFactory.get_url(self.port)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_add_router_interface_missing_params(self):
        response = self.client.post(self.url_add, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_router_interface_both_params(self):
        response = self.client.post(
            self.url_add,
            {
                "subnet": factories.SubNetFactory.get_url(self.subnet),
                "port": factories.PortFactory.get_url(self.port),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_router_interface_wrong_tenant(self):
        other_tenant = factories.TenantFactory()
        other_subnet = factories.SubNetFactory(tenant=other_tenant)
        response = self.client.post(
            self.url_add, {"subnet": factories.SubNetFactory.get_url(other_subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_openstack.backend.OpenStackBackend.remove_router_interface")
    def test_remove_router_interface_with_subnet(self, mock_execute):
        response = self.client.post(
            self.url_remove, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    @mock.patch("waldur_openstack.backend.OpenStackBackend.remove_router_interface")
    def test_remove_router_interface_with_port(self, mock_execute):
        response = self.client.post(
            self.url_remove, {"port": factories.PortFactory.get_url(self.port)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_execute.assert_called_once()

    def test_remove_router_interface_missing_params(self):
        response = self.client.post(self.url_remove, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remove_router_interface_both_params(self):
        response = self.client.post(
            self.url_remove,
            {
                "subnet": factories.SubNetFactory.get_url(self.subnet),
                "port": factories.PortFactory.get_url(self.port),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remove_router_interface_wrong_tenant(self):
        other_tenant = factories.TenantFactory()
        other_port = factories.PortFactory(tenant=other_tenant)
        response = self.client.post(
            self.url_remove, {"port": factories.PortFactory.get_url(other_port)}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
