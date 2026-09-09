"""Creating a subnet without attaching it to a router (#227).

Until now `create_subnet` always attached the new subnet to a router of the
tenant: the only ways to end up with an unattached one were a tenant created
with `skip_creation_of_default_router`, or creating and then disconnecting.

The field is off by default, so every existing caller -- the tenant provisioning
chain included -- keeps the behaviour it had.
"""

from unittest import mock

from rest_framework import status, test

from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend

from . import factories, fixtures


class CreateSubnetWithoutRouterApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.NetworkFactory.get_url(
            network=self.fixture.network, action="create_subnet"
        )

    def _post(self, **extra):
        return self.client.post(self.url, {"name": "test-subnet", **extra})

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_the_flag_reaches_the_executor(self, executor):
        response = self._post(skip_router_connection=True)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        executor.assert_called_once()
        self.assertTrue(executor.call_args.kwargs["skip_router_connection"])

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_omitting_it_keeps_the_previous_behaviour(self, executor):
        response = self._post()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertFalse(executor.call_args.kwargs["skip_router_connection"])

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_it_is_not_stored_on_the_subnet(self, executor):
        """It instructs the executor; it is not a property of the subnet."""
        response = self._post(skip_router_connection=True)

        subnet = models.SubNet.objects.get(uuid=response.data["uuid"])
        self.assertFalse(hasattr(subnet, "skip_router_connection"))
        self.assertNotIn("skip_router_connection", response.data)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_choosing_a_router_and_skipping_the_connection_is_rejected(self, executor):
        response = self._post(
            skip_router_connection=True,
            router=factories.RouterFactory.get_url(self.fixture.router),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("router", response.data)


class SubNetCreateExecutorTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet

    def _signature_kwargs(self, **kwargs):
        from waldur_openstack import executors

        with mock.patch("waldur_core.core.tasks.BackendMethodTask.si") as si:
            executors.SubNetCreateExecutor.get_task_signature(
                self.subnet, "serialized", **kwargs
            )
        return si.call_args.kwargs

    def test_the_default_payload_is_unchanged(self):
        """A worker on the previous release must still be able to run it, so the
        keyword is left out entirely unless it was asked for."""
        self.assertNotIn("skip_router_connection", self._signature_kwargs())

    def test_the_keyword_is_passed_when_set(self):
        self.assertTrue(
            self._signature_kwargs(skip_router_connection=True)[
                "skip_router_connection"
            ]
        )


class CreateSubnetBackendTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.backend_id = "tenant-backend-id"
        self.tenant.save()
        self.network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="network-backend-id",
        )
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="",
            cidr="192.168.99.0/24",
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _create(self, **kwargs):
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
            mock.patch.object(self.backend, "connect_subnet") as connect,
            mock.patch.object(self.backend, "pull_tenant_routers") as pull,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.create_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"}
            }
            self.backend.create_subnet(self.subnet, **kwargs)
        self.subnet.refresh_from_db()
        return connect, pull

    def test_nothing_is_connected_and_the_subnet_says_so(self):
        connect, pull = self._create(skip_router_connection=True)

        connect.assert_not_called()
        pull.assert_not_called()
        self.assertFalse(self.subnet.is_connected)
        self.assertEqual(self.subnet.backend_id, "subnet-backend-id")

    def test_the_default_still_connects(self):
        connect, _ = self._create()

        connect.assert_called_once_with(self.subnet)

    def test_a_tenant_that_skips_routers_also_reports_the_subnet_unconnected(self):
        """Same situation, reached the other way: nothing routes the subnet, so
        the model default of True was simply wrong."""
        self.tenant.skip_creation_of_default_router = True
        self.tenant.save()

        connect, _ = self._create()

        connect.assert_not_called()
        self.assertFalse(self.subnet.is_connected)
