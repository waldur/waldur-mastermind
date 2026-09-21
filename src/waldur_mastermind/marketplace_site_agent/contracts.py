"""STOMP message contracts for site-agent communication.

This module declares the structure of every STOMP message that mastermind
sends to site-agents. The declared contracts are used by:

1. Snapshot tests — detect breaking changes before merge
2. The changelog system — auto-generate entries for contract changes
3. Documentation — generate message reference docs

To add a new message type:
  1. Define it here with MessageContract(...)
  2. Run: uv run pytest src/waldur_mastermind/marketplace_site_agent/tests/test_contracts.py
  3. Update snapshot: uv run python -m waldur_mastermind.marketplace_site_agent.contracts --update

When modifying an existing message:
  - Adding optional fields → non-breaking (old agents ignore them)
  - Adding required fields → BREAKING (old agents will fail)
  - Removing fields → BREAKING (old agents expect them)
  - Changing field types → BREAKING
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SNAPSHOT_PATH = (
    Path(__file__).parent.parent.parent.parent / "contracts" / "stomp-messages.json"
)


@dataclass
class MessageField:
    name: str
    type: str  # "string", "integer", "boolean", "object", "array"
    required: bool = True
    nullable: bool = False
    description: str = ""
    items_type: str = ""  # for arrays
    added_in: str = ""  # version when field was added


@dataclass
class MessageContract:
    name: str
    description: str
    producer: str  # file:function that constructs this message
    fields: list[MessageField] = field(default_factory=list)
    added_in: str = ""  # version when message type was added
    notes: str = ""


# ============================================================
# Contract declarations — one per STOMP message type
# ============================================================

CONTRACTS = [
    MessageContract(
        name="OrderMessage",
        description="Sent when an order changes state (done, pending-provider, pending-consumer).",
        producer="marketplace_site_agent/handlers.py:send_done_order_to_message_queue",
        fields=[
            MessageField("order_uuid", "string", description="UUID of the order"),
            MessageField(
                "order_state", "string", description="Display name of the order state"
            ),
        ],
    ),
    MessageContract(
        name="PendingOrderMessage",
        description="Periodic resend of pending orders (hourly task).",
        producer="marketplace_site_agent/tasks.py:send_messages_about_pending_orders",
        fields=[
            MessageField("order_uuid", "string", description="UUID of the order"),
            MessageField(
                "order_state", "string", description="Display name of the order state"
            ),
            MessageField(
                "sequence_number",
                "integer",
                description="Incremental message sequence number",
            ),
        ],
    ),
    MessageContract(
        name="ResourceUpdateMessage",
        description="Sent when resource downscale, access restriction, or pause state changes.",
        producer="marketplace_site_agent/utils.py:push_resource_update_message",
        fields=[
            MessageField("resource_uuid", "string", description="UUID of the resource"),
            MessageField(
                "resource_backend_id",
                "string",
                description="Backend identifier of the resource",
            ),
            MessageField(
                "downscaled",
                "boolean",
                description="Whether the resource is downscaled",
            ),
            MessageField(
                "restrict_member_access",
                "boolean",
                description="Whether member access is restricted",
            ),
            MessageField(
                "paused", "boolean", description="Whether the resource is paused"
            ),
            MessageField(
                "sequence_number",
                "integer",
                description="Incremental message sequence number",
            ),
        ],
    ),
    MessageContract(
        name="OfferingUserMessage",
        description="Sent when an offering user's username is set or changed.",
        producer="marketplace_site_agent/handlers.py:send_offering_user_username_message",
        fields=[
            MessageField(
                "offering_user_uuid", "string", description="UUID of the offering user"
            ),
            MessageField("user_uuid", "string", description="UUID of the Waldur user"),
            MessageField(
                "username", "string", description="Username of the offering user"
            ),
            MessageField("state", "string", description="State of the offering user"),
            MessageField("action", "string", description="Action type: username_set"),
            MessageField(
                "resource_backend_ids",
                "array",
                items_type="string",
                description="Backend IDs of associated resources",
            ),
        ],
    ),
    MessageContract(
        name="UserRoleMessage",
        description="Sent when a user role is granted or revoked in a project.",
        producer="marketplace_site_agent/handlers.py:process_role_changed",
        fields=[
            MessageField("user_uuid", "string", description="UUID of the user"),
            MessageField("user_username", "string", description="Username of the user"),
            MessageField("project_uuid", "string", description="UUID of the project"),
            MessageField("project_name", "string", description="Name of the project"),
            MessageField("role_name", "string", description="Name of the role"),
            MessageField(
                "granted", "boolean", description="True if granted, False if revoked"
            ),
        ],
    ),
    MessageContract(
        name="UserRoleSyncMessage",
        description="Sent to trigger full user-role sync for a project.",
        producer="marketplace_site_agent/utils.py:push_user_role_sync_message",
        fields=[
            MessageField("project_uuid", "string", description="UUID of the project"),
            MessageField("project_name", "string", description="Name of the project"),
        ],
    ),
    MessageContract(
        name="AccountMessage",
        description="Sent when a service or course account is created or deleted.",
        producer="marketplace_site_agent/handlers.py:send_account_message",
        fields=[
            MessageField("account_uuid", "string", description="UUID of the account"),
            MessageField(
                "account_username", "string", description="Username of the account"
            ),
            MessageField(
                "scope_type", "string", description="Scope type (always 'project')"
            ),
            MessageField("project_uuid", "string", description="UUID of the project"),
            MessageField("project_name", "string", description="Name of the project"),
            MessageField(
                "action", "string", description="Action: 'create' or 'delete'"
            ),
        ],
    ),
    MessageContract(
        name="PeriodicLimitsMessage",
        description="Sent when SLURM policy periodic settings need to be applied.",
        producer="policy/models.py:SlurmPolicy._send_settings_to_site_agent",
        fields=[
            MessageField("resource_uuid", "string", description="UUID of the resource"),
            MessageField(
                "backend_id", "string", description="Backend resource identifier"
            ),
            MessageField("offering_uuid", "string", description="UUID of the offering"),
            MessageField("policy_uuid", "string", description="UUID of the policy"),
            MessageField(
                "action", "string", description="Action: 'apply_periodic_settings'"
            ),
            MessageField(
                "settings",
                "object",
                description="SLURM settings dict (fairshare, limits, thresholds)",
            ),
            MessageField(
                "timestamp",
                "string",
                description="ISO 8601 timestamp for the current period",
            ),
        ],
    ),
]


# The minimum site-agent version for the current contract
MINIMUM_SITE_AGENT_VERSION = "1.0.2"


def check_agent_compatibility():
    """Check all registered site-agents against the current contract.

    Returns a list of warnings for agents that may be incompatible.
    """
    from packaging.version import InvalidVersion, Version

    from waldur_mastermind.marketplace_site_agent.models import AgentIdentity

    min_version = Version(MINIMUM_SITE_AGENT_VERSION)
    warnings = []

    for agent in (
        AgentIdentity.objects.select_related("offering")
        .exclude(version__isnull=True)
        .exclude(version="")
    ):
        try:
            agent_version = Version(agent.version)
        except InvalidVersion:
            warnings.append(
                {
                    "agent": agent.name,
                    "offering": str(agent.offering),
                    "version": agent.version,
                    "status": "unknown",
                    "message": f"Cannot parse version '{agent.version}'",
                }
            )
            continue

        if agent_version < min_version:
            warnings.append(
                {
                    "agent": agent.name,
                    "offering": str(agent.offering),
                    "version": agent.version,
                    "minimum_required": MINIMUM_SITE_AGENT_VERSION,
                    "status": "incompatible",
                    "message": f"Agent '{agent.name}' (v{agent.version}) is below "
                    f"minimum required v{MINIMUM_SITE_AGENT_VERSION} for current STOMP contracts",
                }
            )
        else:
            warnings.append(
                {
                    "agent": agent.name,
                    "offering": str(agent.offering),
                    "version": agent.version,
                    "status": "compatible",
                    "message": f"Agent '{agent.name}' (v{agent.version}) is compatible",
                }
            )

    return warnings


def check_breaking_changes_impact():
    """Compare current contracts against snapshot AND check registered agents.

    Returns a dict with:
    - breaking_changes: list of detected contract changes
    - affected_agents: list of agents that would break
    - minimum_site_agent_version: the version agents need to be
    """
    import json

    if not SNAPSHOT_PATH.exists():
        return {"error": "No snapshot found"}

    with open(SNAPSHOT_PATH) as f:
        old = json.load(f)

    new = contracts_to_schema(CONTRACTS)
    changes = compare_schemas(old, new)
    breaking = [c for c in changes if c["breaking"]]

    if not breaking:
        return {
            "breaking_changes": [],
            "affected_agents": [],
            "minimum_site_agent_version": MINIMUM_SITE_AGENT_VERSION,
        }

    # If there are breaking changes, all agents below the minimum version are affected
    agent_warnings = check_agent_compatibility()
    affected = [w for w in agent_warnings if w["status"] == "incompatible"]

    return {
        "breaking_changes": breaking,
        "affected_agents": affected,
        "compatible_agents": [w for w in agent_warnings if w["status"] == "compatible"],
        "minimum_site_agent_version": MINIMUM_SITE_AGENT_VERSION,
    }


def contracts_to_schema(contracts):
    """Convert contract declarations to a JSON Schema document."""
    messages = {}
    for contract in contracts:
        properties = {}
        required = []
        for f in contract.fields:
            prop = {"type": f.type, "description": f.description}
            if f.nullable:
                prop["nullable"] = True
            if f.type == "array" and f.items_type:
                prop["items"] = {"type": f.items_type}
            properties[f.name] = prop
            if f.required:
                required.append(f.name)

        messages[contract.name] = {
            "type": "object",
            "title": contract.name,
            "description": contract.description,
            "producer": contract.producer,
            "required": sorted(required),
            "properties": properties,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Waldur STOMP Message Contracts",
        "description": "Contracts for mastermind → site-agent STOMP messages. "
        "Auto-generated from contracts.py declarations.",
        "version": "1.0.0",
        "messages": messages,
    }


def compare_schemas(old, new):
    """Compare two contract schemas and return a list of changes."""
    changes = []
    old_msgs = old.get("messages", {})
    new_msgs = new.get("messages", {})

    for name in old_msgs:
        if name not in new_msgs:
            changes.append(
                {
                    "type": "message_removed",
                    "breaking": True,
                    "message": name,
                    "description": f"Message type '{name}' was removed",
                }
            )

    for name in new_msgs:
        if name not in old_msgs:
            changes.append(
                {
                    "type": "message_added",
                    "breaking": False,
                    "message": name,
                    "description": f"New message type '{name}' added",
                }
            )

    for name in old_msgs:
        if name not in new_msgs:
            continue
        old_msg, new_msg = old_msgs[name], new_msgs[name]
        old_props = old_msg.get("properties", {})
        new_props = new_msg.get("properties", {})
        old_req = set(old_msg.get("required", []))
        new_req = set(new_msg.get("required", []))

        for field_name in old_props:
            if field_name not in new_props:
                changes.append(
                    {
                        "type": "field_removed",
                        "breaking": True,
                        "message": name,
                        "field": field_name,
                        "description": f"Field '{field_name}' removed from '{name}'",
                    }
                )

        for field_name in new_props:
            if field_name not in old_props:
                is_req = field_name in new_req
                changes.append(
                    {
                        "type": "field_added",
                        "breaking": is_req,
                        "message": name,
                        "field": field_name,
                        "description": f"{'Required' if is_req else 'Optional'} field '{field_name}' added to '{name}'",
                    }
                )

        for field_name in old_props:
            if field_name not in new_props:
                continue
            if old_props[field_name].get("type") != new_props[field_name].get("type"):
                changes.append(
                    {
                        "type": "field_type_changed",
                        "breaking": True,
                        "message": name,
                        "field": field_name,
                        "from": old_props[field_name].get("type"),
                        "to": new_props[field_name].get("type"),
                        "description": f"Field '{field_name}' in '{name}' changed type "
                        f"from '{old_props[field_name].get('type')}' to '{new_props[field_name].get('type')}'",
                    }
                )

        for field_name in new_req - old_req:
            if field_name in old_props:
                changes.append(
                    {
                        "type": "field_became_required",
                        "breaking": True,
                        "message": name,
                        "field": field_name,
                        "description": f"Field '{field_name}' in '{name}' became required",
                    }
                )

    return changes


def main():
    parser = argparse.ArgumentParser(description="STOMP message contract tool")
    parser.add_argument("--update", action="store_true", help="Update snapshot")
    parser.add_argument("--check", action="store_true", help="Check for changes")
    parser.add_argument(
        "--impact",
        action="store_true",
        help="Check impact on registered agents (requires Django)",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    schema = contracts_to_schema(CONTRACTS)

    if args.update:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump(schema, f, indent=2)
            f.write("\n")
        print(f"Updated: {SNAPSHOT_PATH}")
        return

    if args.check:
        if not SNAPSHOT_PATH.exists():
            print(f"No snapshot at {SNAPSHOT_PATH}. Run with --update first.")
            sys.exit(1)
        with open(SNAPSHOT_PATH) as f:
            old = json.load(f)
        changes = compare_schemas(old, schema)
        if not changes:
            print("No STOMP contract changes.")
            sys.exit(0)
        breaking = [c for c in changes if c["breaking"]]
        non_breaking = [c for c in changes if not c["breaking"]]
        if args.json:
            print(
                json.dumps(
                    {"breaking": breaking, "non_breaking": non_breaking}, indent=2
                )
            )
        else:
            if breaking:
                print(f"BREAKING ({len(breaking)}):")
                for c in breaking:
                    print(f"  ✗ {c['description']}")
            if non_breaking:
                print(f"Non-breaking ({len(non_breaking)}):")
                for c in non_breaking:
                    print(f"  ✓ {c['description']}")
        if breaking:
            print(
                f"\n{len(breaking)} breaking change(s). Site-agents may need updating."
            )
            sys.exit(1)
        sys.exit(0)

    if args.impact:
        import django

        django.setup()
        result = check_breaking_changes_impact()
        affected = result.get("affected_agents", [])
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("error"):
                print(f"Error: {result['error']}")
                sys.exit(1)
            breaking = result.get("breaking_changes", [])
            compatible = result.get("compatible_agents", [])
            if breaking:
                print(f"BREAKING CHANGES ({len(breaking)}):")
                for c in breaking:
                    print(f"  ✗ {c['description']}")
            if affected:
                print(f"\nAFFECTED AGENTS ({len(affected)}):")
                for a in affected:
                    print(f"  ✗ {a['agent']} (v{a['version']}) — {a['offering']}")
                    print(f"    Requires >= v{a['minimum_required']}")
            if compatible:
                print(f"\nCOMPATIBLE AGENTS ({len(compatible)}):")
                for a in compatible:
                    print(f"  ✓ {a['agent']} (v{a['version']}) — {a['offering']}")
            if not breaking:
                print("No breaking contract changes. All agents are compatible.")
        sys.exit(1 if affected else 0)

    # Default: print schema to stdout
    print(json.dumps(schema, indent=2))


if __name__ == "__main__":
    main()
