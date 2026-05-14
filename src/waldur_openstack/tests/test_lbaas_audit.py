"""Audit-log coverage for OpenStack LBaaS lifecycle events."""

from unittest.mock import patch

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType

from . import factories, fixtures


class _BaseLBaaSAuditTest(test.APITestCase):
    EXECUTOR_PATHS: list[str] = []

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        for path in self.EXECUTOR_PATHS:
            p = patch(path, return_value=None)
            p.start()
            self.addCleanup(p.stop)
        self.client.force_authenticate(self.fixture.admin)

    def _events(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type)


class PoolMemberAuditTest(_BaseLBaaSAuditTest):
    """PoolMember is the highest-leverage construct for security audit
    (member IPs/ports decide where traffic flows)."""

    EXECUTOR_PATHS = [
        "waldur_openstack.executors.PoolMemberCreateExecutor.execute",
        "waldur_openstack.executors.PoolMemberUpdateExecutor.execute",
        "waldur_openstack.executors.PoolMemberDeleteExecutor.execute",
    ]

    def setUp(self):
        super().setUp()
        self.load_balancer = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        self.pool = factories.PoolFactory(
            load_balancer=self.load_balancer,
            project=self.fixture.project,
            state=CoreStates.OK,
        )

    def test_create_emits_event_with_actor(self):
        url = factories.PoolMemberFactory.get_list_url()
        response = self.client.post(
            url,
            data={
                "pool": factories.PoolFactory.get_url(self.pool),
                "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                "address": "10.0.0.10",
                "protocol_port": 8080,
                "name": "backend-1",
            },
            format="json",
        )
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )
        events = self._events(EventType.OPENSTACK_POOL_MEMBER_CREATED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["trigger"], "user_action")
        self.assertEqual(ctx["new"]["address"], "10.0.0.10")
        self.assertEqual(ctx["new"]["protocol_port"], 8080)
        self.assertEqual(ctx["user_uuid"], self.fixture.admin.uuid.hex)

    def test_destroy_emits_event(self):
        member = factories.PoolMemberFactory(
            pool=self.pool,
            project=self.fixture.project,
            state=CoreStates.OK,
            address="10.0.0.99",
            protocol_port=8443,
        )
        response = self.client.delete(factories.PoolMemberFactory.get_url(member))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = self._events(EventType.OPENSTACK_POOL_MEMBER_DELETED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["new"]["address"], "10.0.0.99")
        self.assertEqual(ctx["trigger"], "user_action")


class ListenerAuditTest(_BaseLBaaSAuditTest):
    EXECUTOR_PATHS = [
        "waldur_openstack.executors.ListenerCreateExecutor.execute",
        "waldur_openstack.executors.ListenerUpdateExecutor.execute",
        "waldur_openstack.executors.ListenerDeleteExecutor.execute",
    ]

    def setUp(self):
        super().setUp()
        self.load_balancer = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )

    def test_create_emits_event_with_protocol_and_port(self):
        url = factories.ListenerFactory.get_list_url()
        response = self.client.post(
            url,
            data={
                "load_balancer": factories.LoadBalancerFactory.get_url(
                    self.load_balancer
                ),
                "name": "https",
                "protocol": "TCP",
                "protocol_port": 443,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        events = self._events(EventType.OPENSTACK_LISTENER_CREATED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["new"]["protocol"], "TCP")
        self.assertEqual(ctx["new"]["protocol_port"], 443)


class LoadBalancerAuditTest(_BaseLBaaSAuditTest):
    EXECUTOR_PATHS = [
        "waldur_openstack.executors.LoadBalancerCreateExecutor.execute",
        "waldur_openstack.executors.LoadBalancerUpdateExecutor.execute",
        "waldur_openstack.executors.LoadBalancerDeleteExecutor.execute",
    ]

    def test_destroy_emits_event(self):
        lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            state=CoreStates.OK,
        )
        response = self.client.delete(factories.LoadBalancerFactory.get_url(lb))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = self._events(EventType.OPENSTACK_LOAD_BALANCER_DELETED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["trigger"], "user_action")
        self.assertEqual(ctx["name"], lb.name)
