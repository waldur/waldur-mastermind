"""Audit-log coverage for OpenStack security-group rule changes via set_rules."""

from unittest.mock import patch

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType

from . import factories, fixtures


class SecurityGroupSetRulesAuditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.security_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        self.url = factories.SecurityGroupFactory.get_url(
            self.security_group, action="set_rules"
        )
        self.executor_patcher = patch(
            "waldur_openstack.executors.PushSecurityGroupRulesExecutor.execute"
        )
        self.executor_patcher.start()
        self.addCleanup(self.executor_patcher.stop)
        self.client.force_authenticate(self.fixture.admin)

    def _events(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type).order_by("id")

    def test_pure_add_emits_aggregate_event(self):
        response = self.client.post(
            self.url,
            data=[
                {
                    "protocol": "tcp",
                    "from_port": 80,
                    "to_port": 80,
                    "cidr": "0.0.0.0/0",
                },
                {
                    "protocol": "tcp",
                    "from_port": 443,
                    "to_port": 443,
                    "cidr": "0.0.0.0/0",
                },
            ],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["added_count"], 2)
        self.assertEqual(ctx["removed_count"], 0)
        self.assertEqual(ctx["modified_count"], 0)
        self.assertEqual(len(ctx["added_rules"]), 2)
        self.assertEqual(ctx["added_rules"][0]["protocol"], "tcp")
        self.assertEqual(ctx["removed_rules"], [])
        self.assertEqual(ctx["modified_rules"], [])

    def test_pure_remove_emits_aggregate_event(self):
        factories.SecurityGroupRuleFactory(security_group=self.security_group)
        factories.SecurityGroupRuleFactory(security_group=self.security_group)

        response = self.client.post(self.url, data=[], format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["added_count"], 0)
        self.assertEqual(ctx["removed_count"], 2)
        self.assertEqual(ctx["modified_count"], 0)

    def test_in_place_modify_emits_aggregate_event(self):
        rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=80,
            to_port=80,
            cidr="0.0.0.0/0",
        )

        response = self.client.post(
            self.url,
            data=[
                {
                    "id": rule.id,
                    "protocol": "tcp",
                    "from_port": 80,
                    "to_port": 443,
                    "cidr": "0.0.0.0/0",
                }
            ],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        agg_events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(agg_events.count(), 1)
        agg_ctx = agg_events.first().context
        self.assertEqual(agg_ctx["modified_count"], 1)
        modified = agg_ctx["modified_rules"][0]
        self.assertEqual(modified["old"]["to_port"], 80)
        self.assertEqual(modified["new"]["to_port"], 443)
        self.assertIn("to_port", modified["changed_fields"])
        self.assertEqual(agg_ctx["trigger"], "user_action")

    def test_mixed_add_remove_modify(self):
        keep_rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=22,
            to_port=22,
            cidr="10.0.0.0/8",
        )
        modify_rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=80,
            to_port=80,
            cidr="0.0.0.0/0",
        )
        factories.SecurityGroupRuleFactory(  # to be removed
            security_group=self.security_group,
            protocol="udp",
            from_port=53,
            to_port=53,
            cidr="0.0.0.0/0",
        )

        response = self.client.post(
            self.url,
            data=[
                {
                    "id": keep_rule.id,
                    "protocol": "tcp",
                    "from_port": 22,
                    "to_port": 22,
                    "cidr": "10.0.0.0/8",
                },
                {
                    "id": modify_rule.id,
                    "protocol": "tcp",
                    "from_port": 80,
                    "to_port": 443,
                    "cidr": "0.0.0.0/0",
                },
                {
                    "protocol": "icmp",
                    "from_port": -1,
                    "to_port": -1,
                    "cidr": "0.0.0.0/0",
                },
            ],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        ctx = (
            self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
            .first()
            .context
        )
        self.assertEqual(ctx["added_count"], 1)
        self.assertEqual(ctx["removed_count"], 1)
        self.assertEqual(ctx["modified_count"], 1)
        self.assertEqual(ctx["added_rules"][0]["protocol"], "icmp")
        self.assertEqual(ctx["removed_rules"][0]["protocol"], "udp")
        self.assertEqual(ctx["modified_rules"][0]["new"]["to_port"], 443)

    def test_noop_skips_aggregate_event(self):
        # PUT identical rules: nothing changes → no aggregate event.
        rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=80,
            to_port=80,
            cidr="0.0.0.0/0",
        )

        response = self.client.post(
            self.url,
            data=[
                {
                    "id": rule.id,
                    "protocol": "tcp",
                    "from_port": 80,
                    "to_port": 80,
                    "cidr": "0.0.0.0/0",
                }
            ],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED).count(), 0
        )

    def test_actor_context_attached(self):
        # Middleware-injected actor fields must land on the audit event.
        response = self.client.post(
            self.url,
            data=[
                {
                    "protocol": "tcp",
                    "from_port": 22,
                    "to_port": 22,
                    "cidr": "0.0.0.0/0",
                }
            ],
            format="json",
            HTTP_USER_AGENT="audit-test-agent/1.0",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        ctx = (
            self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
            .first()
            .context
        )
        # Auto-attached by CaptureEventContextMiddleware:
        self.assertIn("ip_address", ctx)
        self.assertEqual(ctx["user_uuid"], self.fixture.admin.uuid.hex)
        self.assertEqual(ctx["user_agent"], "audit-test-agent/1.0")


class SecurityGroupCreateAuditTest(test.APITestCase):
    """Verify aggregate event on POST tenants/{uuid}/create_security_group/."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.url = factories.TenantFactory.get_url(
            self.tenant, action="create_security_group"
        )
        self.executor_patcher = patch(
            "waldur_openstack.executors.SecurityGroupCreateExecutor.execute"
        )
        self.executor_patcher.start()
        self.addCleanup(self.executor_patcher.stop)
        self.client.force_authenticate(self.fixture.admin)

    def _events(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type).order_by("id")

    def test_create_with_multiple_rules_emits_single_aggregate_event(self):
        rules = [
            {"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
            {"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"},
            {"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "0.0.0.0/0"},
        ]
        response = self.client.post(
            self.url,
            data={"name": "web-sg", "rules": rules},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["added_count"], 3)
        self.assertEqual(ctx["removed_count"], 0)
        self.assertEqual(ctx["modified_count"], 0)
        self.assertEqual(ctx["trigger"], "user_action")
        # Actor context is auto-attached.
        self.assertEqual(ctx["user_uuid"], self.fixture.admin.uuid.hex)

    def test_create_with_no_rules_emits_no_aggregate_event(self):
        response = self.client.post(
            self.url, data={"name": "empty-sg", "rules": []}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Empty diff → no event noise.
        self.assertEqual(
            self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED).count(), 0
        )


class SecurityGroupDestroyAuditTest(test.APITestCase):
    """Verify aggregate event on DELETE security-group with existing rules."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.security_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        # Two rules to be captured in removed_rules.
        factories.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=22,
            to_port=22,
            cidr="10.0.0.0/8",
            ethertype="IPv4",
            direction="ingress",
        )
        factories.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=443,
            to_port=443,
            cidr="0.0.0.0/0",
            ethertype="IPv4",
            direction="ingress",
        )
        self.url = factories.SecurityGroupFactory.get_url(self.security_group)
        self.executor_patcher = patch(
            "waldur_openstack.executors.SecurityGroupDeleteExecutor.execute"
        )
        self.executor_patcher.start()
        self.addCleanup(self.executor_patcher.stop)
        self.client.force_authenticate(self.fixture.admin)

    def _events(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type).order_by("id")

    def test_destroy_emits_aggregate_event_with_all_rules_removed(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["removed_count"], 2)
        self.assertEqual(ctx["added_count"], 0)
        self.assertEqual(ctx["modified_count"], 0)
        self.assertEqual(ctx["trigger"], "user_action")
        # All rules captured in removed_rules
        self.assertEqual(len(ctx["removed_rules"]), 2)
        ports = {r["from_port"] for r in ctx["removed_rules"]}
        self.assertEqual(ports, {22, 443})
