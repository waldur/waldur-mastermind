from unittest import mock

from keystoneauth1 import exceptions as keystoneauth_exceptions
from novaclient import exceptions as nova_exceptions
from rest_framework import test

from waldur_openstack.backend import OpenStackBackend, OpenStackBackendError
from waldur_openstack.tests import fixtures


def nova_server(server_id, status, flavor, updated="2026-11-27T10:15:00Z"):
    return mock.Mock(id=server_id, status=status, flavor=flavor, updated=updated)


class GetTenantServerComputeTest(test.APITestCase):
    def setUp(self):
        fixtures.mock_session()
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.backend = OpenStackBackend(self.fixture.settings)
        self.nova = mock.patch("waldur_openstack.backend.get_nova_client").start()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_returns_status_and_flavor_size_of_each_server(self):
        self.nova.return_value.servers.list.return_value = [
            nova_server("a", "ACTIVE", {"vcpus": 2, "ram": 2048, "disk": 10}),
            nova_server(
                "b", "SHUTOFF", {"vcpus": 4, "ram": 4096, "disk": 10}, updated=None
            ),
        ]
        servers = self.backend.get_tenant_server_compute(self.tenant)
        self.assertEqual(
            servers,
            [
                {
                    "id": "a",
                    "status": "ACTIVE",
                    "vcpus": 2,
                    "ram": 2048,
                    # Kept as Nova wrote it: billing compares it with the
                    # value an earlier poll read, never with a clock.
                    "updated": "2026-11-27T10:15:00Z",
                },
                # Without a readable change time the caller bills the server.
                {
                    "id": "b",
                    "status": "SHUTOFF",
                    "vcpus": 4,
                    "ram": 4096,
                    "updated": None,
                },
            ],
        )
        # limit=-1 makes novaclient follow every page; without it a tenant with
        # more servers than Nova's max_limit would be billed for the first page.
        self.nova.return_value.servers.list.assert_called_once_with(
            search_opts={"project_id": self.tenant.backend_id, "all_tenants": 1},
            limit=-1,
        )

    def test_server_without_flavor_size_raises(self):
        # Billing such a server as zero would under-bill silently; raising lets
        # the caller fall back to unfiltered usage.
        self.nova.return_value.servers.list.return_value = [
            nova_server("a", "ACTIVE", {"id": "flavor-id"}),
        ]
        with self.assertRaises(OpenStackBackendError):
            self.backend.get_tenant_server_compute(self.tenant)

    def test_nova_error_is_wrapped(self):
        self.nova.return_value.servers.list.side_effect = (
            nova_exceptions.ClientException(500)
        )
        with self.assertRaises(OpenStackBackendError):
            self.backend.get_tenant_server_compute(self.tenant)

    def test_connection_failure_is_wrapped(self):
        # Transport errors come from keystoneauth, not novaclient.
        self.nova.return_value.servers.list.side_effect = (
            keystoneauth_exceptions.ConnectFailure("Nova unreachable")
        )
        with self.assertRaises(OpenStackBackendError):
            self.backend.get_tenant_server_compute(self.tenant)

    def test_tenant_without_backend_id_raises(self):
        # novaclient drops an empty project_id filter, so the call would list
        # every server in the cloud.
        self.tenant.backend_id = ""
        with self.assertRaises(OpenStackBackendError):
            self.backend.get_tenant_server_compute(self.tenant)
        self.nova.return_value.servers.list.assert_not_called()
