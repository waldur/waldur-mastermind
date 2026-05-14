"""Audit-log coverage for security-group attachment changes.

Covers:
- Instance.update_security_groups
- Port.update_security_groups
- LoadBalancer.set_security_groups (operates on the LB's VIP port).
"""

from unittest.mock import patch

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType

from . import factories, fixtures


def _events(event_type):
    return logging_models.Event.objects.filter(event_type=event_type).order_by("id")


class InstanceSecurityGroupsAttachmentAuditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        # Pre-seed two SGs on the instance for the remove/mixed cases.
        self.sg_a, self.sg_b = factories.SecurityGroupFactory.create_batch(
            2, tenant=self.fixture.tenant
        )
        self.instance.security_groups.add(self.sg_a, self.sg_b)
        self.url = factories.InstanceFactory.get_url(
            self.instance, action="update_security_groups"
        )
        self.executor_patcher = patch(
            "waldur_openstack.executors.InstanceUpdateSecurityGroupsExecutor.execute"
        )
        self.executor_patcher.start()
        self.addCleanup(self.executor_patcher.stop)
        self.client.force_authenticate(self.fixture.admin)

    def _post(self, sgs):
        return self.client.post(
            self.url,
            {
                "security_groups": [
                    factories.SecurityGroupFactory.get_url(sg) for sg in sgs
                ]
            },
        )

    def test_pure_add_emits_aggregate_event(self):
        new_sg = factories.SecurityGroupFactory(tenant=self.fixture.tenant)
        response = self._post([self.sg_a, self.sg_b, new_sg])
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = _events(EventType.OPENSTACK_INSTANCE_SECURITY_GROUPS_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["added_count"], 1)
        self.assertEqual(ctx["removed_count"], 0)
        self.assertEqual(ctx["added_security_groups"][0]["uuid"], str(new_sg.uuid))
        self.assertEqual(ctx["removed_security_groups"], [])
        self.assertEqual(ctx["trigger"], "user_action")

    def test_pure_remove_emits_aggregate_event(self):
        response = self._post([self.sg_a])
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = _events(EventType.OPENSTACK_INSTANCE_SECURITY_GROUPS_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["added_count"], 0)
        self.assertEqual(ctx["removed_count"], 1)
        self.assertEqual(ctx["removed_security_groups"][0]["uuid"], str(self.sg_b.uuid))

    def test_mixed_add_remove_emits_event(self):
        new_sg = factories.SecurityGroupFactory(tenant=self.fixture.tenant)
        response = self._post([self.sg_a, new_sg])  # keeps A, drops B, adds new
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        ctx = (
            _events(EventType.OPENSTACK_INSTANCE_SECURITY_GROUPS_CHANGED)
            .first()
            .context
        )
        self.assertEqual(ctx["added_count"], 1)
        self.assertEqual(ctx["removed_count"], 1)
        added_uuids = {sg["uuid"] for sg in ctx["added_security_groups"]}
        removed_uuids = {sg["uuid"] for sg in ctx["removed_security_groups"]}
        self.assertEqual(added_uuids, {str(new_sg.uuid)})
        self.assertEqual(removed_uuids, {str(self.sg_b.uuid)})

    def test_noop_emits_no_event(self):
        response = self._post([self.sg_a, self.sg_b])  # identical set
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            _events(EventType.OPENSTACK_INSTANCE_SECURITY_GROUPS_CHANGED).count(), 0
        )


class PortSecurityGroupsAttachmentAuditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.port = self.fixture.port
        self.sg_a, self.sg_b = factories.SecurityGroupFactory.create_batch(
            2, tenant=self.fixture.tenant
        )
        self.port.security_groups.add(self.sg_a, self.sg_b)
        self.url = factories.PortFactory.get_url(self.port, "update_security_groups")
        self.client.force_authenticate(self.fixture.admin)

    def _post(self, sgs):
        return self.client.post(
            self.url,
            {
                "security_groups": [
                    factories.SecurityGroupFactory.get_url(sg) for sg in sgs
                ]
            },
        )

    def test_pure_add_emits_event(self):
        new_sg = factories.SecurityGroupFactory(tenant=self.fixture.tenant)
        response = self._post([self.sg_a, self.sg_b, new_sg])
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        ctx = _events(EventType.OPENSTACK_PORT_SECURITY_GROUPS_CHANGED).first().context
        self.assertEqual(ctx["added_count"], 1)
        self.assertEqual(ctx["removed_count"], 0)
        self.assertEqual(ctx["added_security_groups"][0]["uuid"], str(new_sg.uuid))

    def test_pure_remove_emits_event(self):
        response = self._post([self.sg_a])
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        ctx = _events(EventType.OPENSTACK_PORT_SECURITY_GROUPS_CHANGED).first().context
        self.assertEqual(ctx["added_count"], 0)
        self.assertEqual(ctx["removed_count"], 1)

    def test_noop_emits_no_event(self):
        response = self._post([self.sg_a, self.sg_b])
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            _events(EventType.OPENSTACK_PORT_SECURITY_GROUPS_CHANGED).count(), 0
        )


class LoadBalancerSecurityGroupsAttachmentAuditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.vip_port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="vip_port_for_audit",
        )
        self.lb = factories.LoadBalancerFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            vip_port=self.vip_port,
            state=CoreStates.OK,
        )
        # Pre-seed one SG attached to the VIP port to exercise add/remove diff.
        self.existing_sg = factories.SecurityGroupFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.vip_port.security_groups.add(self.existing_sg)
        self.url = factories.LoadBalancerFactory.get_url(self.lb, "set_security_groups")
        self.executor_patcher = patch(
            "waldur_openstack.executors.LoadBalancerSetSecurityGroupsExecutor.execute"
        )
        self.executor_patcher.start()
        self.addCleanup(self.executor_patcher.stop)
        self.client.force_authenticate(self.fixture.admin)

    def _post(self, sgs):
        return self.client.post(
            self.url,
            {
                "security_groups": [
                    factories.SecurityGroupFactory.get_url(sg) for sg in sgs
                ]
            },
        )

    def test_replace_sg_emits_add_and_remove(self):
        new_sg = factories.SecurityGroupFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        response = self._post([new_sg])  # replaces existing_sg
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = _events(EventType.OPENSTACK_LOAD_BALANCER_SECURITY_GROUPS_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["added_count"], 1)
        self.assertEqual(ctx["removed_count"], 1)
        self.assertEqual(ctx["added_security_groups"][0]["uuid"], str(new_sg.uuid))
        self.assertEqual(
            ctx["removed_security_groups"][0]["uuid"], str(self.existing_sg.uuid)
        )
        self.assertEqual(ctx["trigger"], "user_action")

    def test_clear_all_emits_remove(self):
        response = self._post([])
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        ctx = (
            _events(EventType.OPENSTACK_LOAD_BALANCER_SECURITY_GROUPS_CHANGED)
            .first()
            .context
        )
        self.assertEqual(ctx["added_count"], 0)
        self.assertEqual(ctx["removed_count"], 1)

    def test_noop_emits_no_event(self):
        response = self._post([self.existing_sg])  # already attached
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            _events(EventType.OPENSTACK_LOAD_BALANCER_SECURITY_GROUPS_CHANGED).count(),
            0,
        )
