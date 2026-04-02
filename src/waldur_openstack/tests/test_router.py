from unittest import mock

from ddt import data, ddt
from django.test import override_settings
from rest_framework import status, test

from . import factories, fixtures


class BaseRouterTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        self.mock = mock.patch("waldur_openstack.backend.OpenStackBackend.get_free_ip")
        self.mock_get_free_ip = self.mock.start()
        self.mock_get_free_ip.return_value = "1.1.1.1"
        self.mock = mock.patch("waldur_openstack.backend.OpenStackBackend.create_port")
        self.mock_create_port = self.mock.start()


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

    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.add_router_interface")
    @mock.patch(
        "waldur_openstack.backend.OpenStackBackend.get_free_ip",
        return_value="192.168.1.10",
    )
    def test_add_router_interface_with_subnet(
        self, mock_get_free_ip, mock_add, mock_pull
    ):
        response = self.client.post(
            self.url_add, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        mock_add.assert_called_once()
        mock_pull.assert_called_once()

    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.add_router_interface")
    def test_add_router_interface_with_port(self, mock_add, mock_pull):
        response = self.client.post(
            self.url_add, {"port": factories.PortFactory.get_url(self.port)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_add.assert_called_once()
        mock_pull.assert_called_once()

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

    @override_settings(task_always_eager=True)
    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.remove_router_interface")
    def test_remove_router_interface_with_subnet(self, mock_remove, mock_pull):
        response = self.client.post(
            self.url_remove, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_remove.assert_called_once()
        mock_pull.assert_called_once()

    @override_settings(task_always_eager=True)
    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.remove_router_interface")
    def test_remove_router_interface_with_port(self, mock_remove, mock_pull):
        response = self.client.post(
            self.url_remove, {"port": factories.PortFactory.get_url(self.port)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_remove.assert_called_once()
        mock_pull.assert_called_once()

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


@ddt
@mock.patch("waldur_openstack.executors.RouterCreateExecutor.execute")
class RouterCreateTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.url = factories.RouterFactory.get_list_url()

        self.valid_data = {
            "name": "Test Router",
            "tenant": factories.TenantFactory.get_url(self.fixture.tenant),
        }

    @data("admin", "manager", "staff", "owner")
    def test_user_can_create_router(self, user, create_port_executor_mock):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        create_port_executor_mock.assert_called_once()

    @data("user", "member", "global_support")
    def test_user_can_not_create_router(self, user, create_port_executor_mock):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()

    def test_router_cannot_be_created_without_name(self, create_port_executor_mock):
        self.client.force_authenticate(self.fixture.owner)
        data = self.valid_data.copy()
        data.pop("name")
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()

    def test_router_cannot_be_created_without_tenant(self, create_port_executor_mock):
        self.client.force_authenticate(self.fixture.owner)
        data = self.valid_data.copy()
        data.pop("tenant")
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()
