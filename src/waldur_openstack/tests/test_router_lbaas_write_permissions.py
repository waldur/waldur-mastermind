"""Writes to routers and load balancers are for those who manage their project.

Seeing one is not enough: project members, customer support and global support
see them and may not change them.
"""

from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core import executors as core_executors
from waldur_core.core.enums import CoreStates
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.models import Port

from . import factories, fixtures

ONLY_SEE_IT = ("member", "customer_support", "global_support")
MANAGE_ITS_PROJECT = ("staff", "owner", "admin", "manager")


class WritePermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        # Every executor of these objects, so nothing reaches the cloud and a
        # refused request can be shown to have scheduled nothing.
        self.executors = []
        for patcher in (
            mock.patch.object(core_executors.BaseExecutor, "execute"),
            mock.patch.object(core_executors.UpdateExecutor, "execute"),
        ):
            self.addCleanup(patcher.stop)
            self.executors.append(patcher.start())

    def scoped(self, **kwargs):
        return dict(
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
            **kwargs,
        )

    def assert_refused(self, user, requests):
        self.client.force_authenticate(getattr(self.fixture, user))
        for method, url, payload in requests:
            with self.subTest(user=user, method=method, url=url):
                response = getattr(self.client, method)(url, payload, format="json")
                self.assertEqual(
                    response.status_code, status.HTTP_403_FORBIDDEN, response.data
                )
        for execute in self.executors:
            execute.assert_not_called()

    def assert_scheduled(self):
        self.assertTrue(any(execute.called for execute in self.executors))


@ddt
class RouterWritePermissionTest(WritePermissionTest):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(**self.scoped(tenant=self.fixture.tenant))
        self.subnet = factories.SubNetFactory(**self.scoped(tenant=self.fixture.tenant))
        for name in (
            "get_free_ip",
            "create_port",
            "add_router_interface",
            "remove_router_interface",
            "pull_tenant_routers",
        ):
            patcher = mock.patch.object(OpenStackBackend, name)
            self.addCleanup(patcher.stop)
            setattr(self, name, patcher.start())
        self.get_free_ip.return_value = "192.168.1.10"

    def url(self, action=None):
        return factories.RouterFactory.get_url(self.router, action=action)

    @data(*ONLY_SEE_IT)
    def test_who_only_sees_it_cannot_change_it(self, user):
        subnet = {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        self.assert_refused(
            user,
            [
                ("post", self.url("add_router_interface"), subnet),
                ("post", self.url("remove_router_interface"), subnet),
                ("post", self.url("set_routes"), {"routes": []}),
                ("delete", self.url(), None),
            ],
        )
        self.create_port.assert_not_called()
        self.add_router_interface.assert_not_called()
        self.assertFalse(Port.objects.exists())

    @data(*MANAGE_ITS_PROJECT)
    def test_who_manages_its_project_can_add_an_interface(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(
            self.url("add_router_interface"),
            {"subnet": factories.SubNetFactory.get_url(self.subnet)},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.add_router_interface.assert_called_once()

    @data(*MANAGE_ITS_PROJECT)
    def test_who_manages_its_project_can_delete_it(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url())

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assert_scheduled()


@ddt
class LoadBalancerWritePermissionTest(WritePermissionTest):
    def setUp(self):
        super().setUp()
        self.load_balancer = factories.LoadBalancerFactory(
            **self.scoped(tenant=self.fixture.tenant, backend_id="lb-backend-id")
        )
        self.pool = factories.PoolFactory(
            **self.scoped(load_balancer=self.load_balancer, backend_id="pool-id")
        )
        self.listener = factories.ListenerFactory(
            **self.scoped(load_balancer=self.load_balancer, backend_id="listener-id")
        )
        self.member = factories.PoolMemberFactory(
            **self.scoped(pool=self.pool, backend_id="member-id")
        )
        self.health_monitor = factories.HealthMonitorFactory(
            **self.scoped(pool=self.pool, backend_id="monitor-id")
        )
        self.objects = [
            (factories.LoadBalancerFactory, self.load_balancer),
            (factories.PoolFactory, self.pool),
            (factories.ListenerFactory, self.listener),
            (factories.PoolMemberFactory, self.member),
            (factories.HealthMonitorFactory, self.health_monitor),
        ]

    @data(*ONLY_SEE_IT)
    def test_who_only_sees_them_cannot_change_them(self, user):
        requests = []
        for factory, obj in self.objects:
            url = factory.get_url(obj)
            requests += [
                ("patch", url, {"name": "renamed"}),
                ("post", factory.get_url(obj, action="pull"), None),
                ("delete", url, None),
            ]
        lb = self.load_balancer
        requests += [
            (
                "post",
                factories.LoadBalancerFactory.get_url(lb, "attach_floating_ip"),
                {},
            ),
            (
                "post",
                factories.LoadBalancerFactory.get_url(lb, "detach_floating_ip"),
                {},
            ),
            (
                "post",
                factories.LoadBalancerFactory.get_url(lb, "set_security_groups"),
                {"security_groups": []},
            ),
        ]

        self.assert_refused(user, requests)

        for _, obj in self.objects:
            obj.refresh_from_db()
            self.assertNotEqual(obj.name, "renamed")

    @data(*MANAGE_ITS_PROJECT)
    def test_who_manages_their_project_can_rename_and_pull_them(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        for factory, obj in self.objects:
            with self.subTest(obj=type(obj).__name__):
                renamed = self.client.patch(
                    factory.get_url(obj), {"name": "renamed"}, format="json"
                )
                pulled = self.client.post(factory.get_url(obj, action="pull"))
                self.assertEqual(renamed.status_code, status.HTTP_200_OK, renamed.data)
                self.assertEqual(
                    pulled.status_code, status.HTTP_202_ACCEPTED, pulled.data
                )

    @data(*MANAGE_ITS_PROJECT)
    def test_who_manages_their_project_can_delete_them(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        for factory, obj in reversed(self.objects):
            with self.subTest(obj=type(obj).__name__):
                response = self.client.delete(factory.get_url(obj))
                self.assertEqual(
                    response.status_code, status.HTTP_202_ACCEPTED, response.data
                )
        self.assert_scheduled()
