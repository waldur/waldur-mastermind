import json
from pathlib import Path

from django.test import TestCase

from waldur_mastermind.marketplace_site_agent.contracts import (
    CONTRACTS,
    compare_schemas,
    contracts_to_schema,
)

SNAPSHOT_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "contracts"
    / "stomp-messages.json"
)


class ContractSnapshotTest(TestCase):
    def test_snapshot_exists(self):
        self.assertTrue(
            SNAPSHOT_PATH.exists(),
            f"STOMP contract snapshot not found at {SNAPSHOT_PATH}. "
            "Run: uv run python -m waldur_mastermind.marketplace_site_agent.contracts --update",
        )

    def test_no_breaking_changes(self):
        with open(SNAPSHOT_PATH) as f:
            old = json.load(f)
        new = contracts_to_schema(CONTRACTS)
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(
            breaking,
            [],
            "Breaking STOMP contract changes detected:\n"
            + "\n".join(f"  - {c['description']}" for c in breaking)
            + "\n\nUpdate snapshot with: "
            "uv run python -m waldur_mastermind.marketplace_site_agent.contracts --update",
        )

    def test_all_contracts_in_snapshot(self):
        with open(SNAPSHOT_PATH) as f:
            snapshot = json.load(f)
        snapshot_names = set(snapshot.get("messages", {}).keys())
        contract_names = {c.name for c in CONTRACTS}
        missing = contract_names - snapshot_names
        self.assertEqual(
            missing,
            set(),
            f"New message types not in snapshot: {missing}. Update snapshot.",
        )


class AgentCompatibilityTest(TestCase):
    def setUp(self):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )
        from waldur_mastermind.marketplace_site_agent.models import AgentIdentity

        self.offering = marketplace_factories.OfferingFactory()
        self.old_agent = AgentIdentity.objects.create(
            name="Old SLURM Agent",
            offering=self.offering,
            version="0.9.0",
        )
        self.current_agent = AgentIdentity.objects.create(
            name="Current SLURM Agent",
            offering=self.offering,
            version="1.0.4",
        )

    def test_detects_incompatible_agent(self):
        from waldur_mastermind.marketplace_site_agent.contracts import (
            check_agent_compatibility,
        )

        warnings = check_agent_compatibility()
        by_status = {w["agent"]: w["status"] for w in warnings}
        self.assertEqual(by_status["Old SLURM Agent"], "incompatible")
        self.assertEqual(by_status["Current SLURM Agent"], "compatible")

    def test_incompatible_agent_has_minimum_version(self):
        from waldur_mastermind.marketplace_site_agent.contracts import (
            MINIMUM_SITE_AGENT_VERSION,
            check_agent_compatibility,
        )

        warnings = check_agent_compatibility()
        old_warning = next(w for w in warnings if w["agent"] == "Old SLURM Agent")
        self.assertEqual(old_warning["minimum_required"], MINIMUM_SITE_AGENT_VERSION)


class ContractComparisonTest(TestCase):
    def test_detects_field_removal(self):
        old = {
            "messages": {
                "Msg": {
                    "required": ["a", "b"],
                    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                }
            }
        }
        new = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "field_removed")

    def test_detects_required_field_addition(self):
        old = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        new = {
            "messages": {
                "Msg": {
                    "required": ["a", "b"],
                    "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                }
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "field_added")

    def test_detects_type_change(self):
        old = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        new = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "integer"}}}
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "field_type_changed")

    def test_optional_field_not_breaking(self):
        old = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        new = {
            "messages": {
                "Msg": {
                    "required": ["a"],
                    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                }
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 0)

    def test_detects_message_removal(self):
        old = {"messages": {"Msg": {"required": [], "properties": {}}}}
        new = {"messages": {}}
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "message_removed")
