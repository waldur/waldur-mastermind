"""Live-lab validation for security-group audit logging.

Exercises the Waldur OpenStack backend against a real Neutron instance using
non-admin tenant credentials sourced from ``.secrets/lab-tenant-creds.env`` at
the workspace root. Skipped when that file is absent.

Run with::

    DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local \
        uv run pytest src/waldur_openstack/tests/test_security_group_audit_lab.py \
        -v --reuse-db -m lab
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_openstack.backend import OpenStackBackend, get_tenant_session
from waldur_openstack.session import get_neutron_client

from . import factories, fixtures

# test file is at /waldur/waldur-mastermind/src/waldur_openstack/tests/...
# workspace root is 4 levels up.
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
CREDS_FILE = WORKSPACE_ROOT / ".secrets" / "lab-tenant-creds.env"

pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(
        not CREDS_FILE.exists(),
        reason=f"No lab credentials at {CREDS_FILE} — run scripts/lab-provision-audit-tenant.sh first.",
    ),
]


def _load_creds() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in CREDS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


class LabSecurityGroupAuditTest(test.APITestCase):
    """End-to-end push/pull events against a real OpenStack security group."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creds = _load_creds()
        cls.backend_sg_id = cls.creds["SG_ID"]
        cls.backend_project_id = cls.creds["OS_PROJECT_ID"]

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        settings = self.fixture.settings
        settings.backend_url = self.creds["OS_AUTH_URL"]
        settings.username = self.creds["OS_USERNAME"]
        settings.password = self.creds["OS_PASSWORD"]
        settings.domain = self.creds["OS_USER_DOMAIN_NAME"]
        settings.save()

        tenant = self.fixture.tenant
        tenant.backend_id = self.backend_project_id
        tenant.user_username = self.creds["OS_USERNAME"]
        tenant.user_password = self.creds["OS_PASSWORD"]
        tenant.save()
        self.tenant = tenant

        self.security_group = factories.SecurityGroupFactory(
            service_settings=settings,
            project=self.fixture.project,
            tenant=tenant,
            name=self.creds["SG_NAME"],
            backend_id=self.backend_sg_id,
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(settings)
        self._reset_lab_sg_to_defaults()
        # Make local rules mirror lab default state (2 egress rules) so each
        # test starts from the same baseline. We use pull_security_group to
        # populate them, then clear event history for cleaner assertions.
        self.backend.pull_security_group(self.security_group)
        logging_models.Event.objects.all().delete()

    def tearDown(self):
        # Dump every event still in the DB *before* the transaction rolls back,
        # so we have a durable record of what got emitted by each test.
        self._dump_events()
        # Leave the lab SG in its provisioned-default state for subsequent runs.
        self._reset_lab_sg_to_defaults()
        super().tearDown()

    def _dump_events(self):
        out_dir = WORKSPACE_ROOT / ".audit-snapshots"
        out_dir.mkdir(exist_ok=True)
        # Build a safe filename from the test id (e.g.
        # "LabSecurityGroupAuditTest.test_push_emits_backend_sync_create_event")
        safe_name = self.id().split(".")[-1]
        out_path = out_dir / f"{safe_name}.json"
        payload = {
            "test_id": self.id(),
            "dumped_at": datetime.utcnow().isoformat() + "Z",
            "event_count": logging_models.Event.objects.count(),
            "events": [
                {
                    "id": ev.id,
                    "created": ev.created.isoformat(),
                    "event_type": ev.event_type,
                    "message": ev.message,
                    "context": ev.context,
                }
                for ev in logging_models.Event.objects.order_by("id")
            ],
        }
        out_path.write_text(json.dumps(payload, indent=2, default=str))

    def _neutron(self):
        session = get_tenant_session(self.tenant)
        return get_neutron_client(session)

    def _remote_rules(self):
        neutron = self._neutron()
        return neutron.show_security_group(self.backend_sg_id)["security_group"][
            "security_group_rules"
        ]

    def _reset_lab_sg_to_defaults(self):
        """Delete every rule on the lab SG except the two default IPv4/IPv6 egress rules."""
        neutron = self._neutron()
        for rule in self._remote_rules():
            is_default_egress = (
                rule["direction"] == "egress"
                and rule["protocol"] is None
                and rule["port_range_min"] is None
                and rule["port_range_max"] is None
                and rule["remote_ip_prefix"] is None
            )
            if not is_default_egress:
                neutron.delete_security_group_rule(rule["id"])

    def _events(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type).order_by("id")

    def _assert_event_feed_includes_tenant(self, event):
        """Verify the event has a Feed row pointing at the tenant.

        This is what makes the event surface on the marketplace.Resource events
        tab (via the ScopeMixin OR-branch in logging.filters).
        """
        from django.contrib.contenttypes.models import ContentType

        from waldur_openstack.models import Tenant

        tenant_ct = ContentType.objects.get_for_model(Tenant)
        self.assertTrue(
            logging_models.Feed.objects.filter(
                event=event,
                content_type=tenant_ct,
                object_id=self.tenant.id,
            ).exists(),
            f"event {event.id} ({event.event_type}) is missing a Feed entry pointing at tenant {self.tenant.id}",
        )

    # ------------------------------------------------------------------
    # Flow B: change happens directly in OpenStack — pull emits one aggregate
    # event covering the entire reconciliation, with trigger=backend_sync.
    # ------------------------------------------------------------------
    def test_pull_emits_aggregate_event_when_rule_added_remotely(self):
        neutron = self._neutron()
        sec_group_rule = neutron.create_security_group_rule(
            {
                "security_group_rule": {
                    "security_group_id": self.backend_sg_id,
                    "direction": "ingress",
                    "ethertype": "IPv4",
                    "protocol": "tcp",
                    "port_range_min": 443,
                    "port_range_max": 443,
                    "remote_ip_prefix": "0.0.0.0/0",
                }
            }
        )["security_group_rule"]
        self.assertFalse(
            self.security_group.rules.filter(backend_id=sec_group_rule["id"]).exists()
        )

        self.backend.pull_security_group(self.security_group)

        self.assertTrue(
            self.security_group.rules.filter(backend_id=sec_group_rule["id"]).exists()
        )

        # Exactly one aggregate event covers the pull, with the new rule in added_rules.
        agg_events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(agg_events.count(), 1)
        event = agg_events.first()
        self.assertEqual(event.context["trigger"], "backend_sync")
        self.assertEqual(event.context["added_count"], 1)
        self.assertEqual(event.context["removed_count"], 0)
        added = event.context["added_rules"][0]
        self.assertEqual(added["from_port"], 443)
        self.assertEqual(added["to_port"], 443)
        self.assertEqual(added["protocol"], "tcp")
        self._assert_event_feed_includes_tenant(event)

    def test_pull_emits_aggregate_event_when_rule_removed_remotely(self):
        neutron = self._neutron()
        remote = neutron.create_security_group_rule(
            {
                "security_group_rule": {
                    "security_group_id": self.backend_sg_id,
                    "direction": "ingress",
                    "ethertype": "IPv4",
                    "protocol": "icmp",
                    "remote_ip_prefix": "0.0.0.0/0",
                }
            }
        )["security_group_rule"]
        # Sync the new rule into local DB, then clear events to isolate the next pull.
        self.backend.pull_security_group(self.security_group)
        self.assertTrue(
            self.security_group.rules.filter(backend_id=remote["id"]).exists()
        )
        logging_models.Event.objects.all().delete()

        # Delete the rule remotely and pull again.
        neutron.delete_security_group_rule(remote["id"])
        self.backend.pull_security_group(self.security_group)

        self.assertFalse(
            self.security_group.rules.filter(backend_id=remote["id"]).exists()
        )

        agg_events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(agg_events.count(), 1)
        event = agg_events.first()
        self.assertEqual(event.context["trigger"], "backend_sync")
        self.assertEqual(event.context["removed_count"], 1)
        self.assertEqual(event.context["added_count"], 0)
        removed = event.context["removed_rules"][0]
        self.assertEqual(removed["protocol"], "icmp")
        self._assert_event_feed_includes_tenant(event)

    def test_pull_with_no_remote_changes_does_not_emit_event(self):
        # Baseline pull from setUp already mirrored remote state; a second
        # pull with no remote changes should be silent.
        logging_models.Event.objects.all().delete()
        self.backend.pull_security_group(self.security_group)
        self.assertEqual(
            self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED).count(), 0
        )

    # ------------------------------------------------------------------
    # Multi-rule API call: verifies the aggregate event payload structure.
    # Goes through the full HTTP/viewset path so we exercise the diff capture
    # in OpenStackSecurityGroupRuleListUpdateSerializer + set_rules viewset.
    # ------------------------------------------------------------------
    def test_set_rules_with_multiple_rules_emits_aggregate_event(self):
        from . import factories as f

        # Clear the rules that setUp's pull imported from the lab — they would
        # all count as "removed" in the diff below otherwise. We are not
        # exercising the real backend push in this test; the executor is mocked.
        self.security_group.rules.all().delete()
        logging_models.Event.objects.all().delete()

        # Seed two pre-existing local rules: one we'll keep, one we'll modify.
        keep_rule = f.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=22,
            to_port=22,
            cidr="10.0.0.0/8",
            ethertype="IPv4",
            direction="ingress",
        )
        modify_rule = f.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="tcp",
            from_port=80,
            to_port=80,
            cidr="0.0.0.0/0",
            ethertype="IPv4",
            direction="ingress",
        )
        # And a third rule we'll let be removed by leaving it out of the PUT body.
        f.SecurityGroupRuleFactory(
            security_group=self.security_group,
            protocol="udp",
            from_port=53,
            to_port=53,
            cidr="0.0.0.0/0",
            ethertype="IPv4",
            direction="ingress",
        )

        url = f.SecurityGroupFactory.get_url(self.security_group, action="set_rules")
        self.client.force_authenticate(self.fixture.admin)

        # Mock the executor — we only care about the API-layer event here, not the
        # downstream push to OpenStack (covered by the other lab tests).
        with patch("waldur_openstack.executors.PushSecurityGroupRulesExecutor.execute"):
            response = self.client.post(
                url,
                data=[
                    # KEEP: same rule, no changes.
                    {
                        "id": keep_rule.id,
                        "protocol": "tcp",
                        "from_port": 22,
                        "to_port": 22,
                        "cidr": "10.0.0.0/8",
                        "ethertype": "IPv4",
                        "direction": "ingress",
                    },
                    # MODIFY: port range expanded.
                    {
                        "id": modify_rule.id,
                        "protocol": "tcp",
                        "from_port": 80,
                        "to_port": 8443,
                        "cidr": "0.0.0.0/0",
                        "ethertype": "IPv4",
                        "direction": "ingress",
                    },
                    # ADD: brand-new ICMP rule.
                    {
                        "protocol": "icmp",
                        "from_port": -1,
                        "to_port": -1,
                        "cidr": "0.0.0.0/0",
                        "ethertype": "IPv4",
                        "direction": "ingress",
                    },
                    # ADD: brand-new HTTPS rule.
                    {
                        "protocol": "tcp",
                        "from_port": 443,
                        "to_port": 443,
                        "cidr": "0.0.0.0/0",
                        "ethertype": "IPv4",
                        "direction": "ingress",
                    },
                ],
                format="json",
                HTTP_USER_AGENT="audit-multi-rule-test/1.0",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        agg_events = self._events(EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED)
        self.assertEqual(agg_events.count(), 1)
        ctx = agg_events.first().context

        # Summary correctly reports: 2 added (icmp + 443), 1 removed (udp 53), 1 modified (port 80->8443).
        self.assertEqual(ctx["added_count"], 2)
        self.assertEqual(ctx["removed_count"], 1)
        self.assertEqual(ctx["modified_count"], 1)
        self.assertEqual(len(ctx["added_rules"]), 2)
        self.assertEqual(len(ctx["removed_rules"]), 1)
        self.assertEqual(len(ctx["modified_rules"]), 1)

        # Modified entry carries old + new shapes and the changed-field list.
        modified = ctx["modified_rules"][0]
        self.assertEqual(modified["old"]["to_port"], 80)
        self.assertEqual(modified["new"]["to_port"], 8443)
        self.assertIn("to_port", modified["changed_fields"])
        self.assertEqual(ctx["trigger"], "user_action")
