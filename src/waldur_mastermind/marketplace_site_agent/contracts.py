"""STOMP message contracts for site-agent communication.

This module declares the structure of every STOMP message that mastermind
sends to site-agents. The declarations are the source of truth for:

1. contracts/asyncapi.yaml — an AsyncAPI 3.0 document generated from them,
   for consumers and standard AsyncAPI tooling (validation, docs, codegen)
2. Snapshot tests — detect breaking changes against that document before merge
3. Coverage and conformance tests — every event type and producer is declared,
   and producers send what their contract says

The contracts describe what a unified consumer queue (EventConsumer) receives:
the producer's payload, the envelope (ENVELOPE_FIELDS) and the context fields
the payload enrichers add. Agents that still subscribe through the deprecated
per-offering EventSubscription path (site agents before the unified queue,
1.0.7) receive only the producer's payload plus offering_uuid: no object_type,
schema_version or emitted_at, and none of the enricher fields. The
conformance test checks the unified path only.

To add a new message type:
  1. Define it here with MessageContract(...), naming its object_types and every
     function that publishes it in producers
  2. Add a case for it to tests/test_contract_conformance.py, which calls the
     producer and checks the delivered payload against the contract
  3. Run: uv run pytest src/waldur_mastermind/marketplace_site_agent/tests/test_contracts.py
     src/waldur_mastermind/marketplace_site_agent/tests/test_contract_conformance.py
  4. Regenerate contracts/asyncapi.yaml:
     uv run python -m waldur_mastermind.marketplace_site_agent.contracts --update

test_contract_conformance.py also fails when a function starts publishing a
site-agent message without being listed as a producer, and test_contracts.py
when an ObservableObjectType has neither a contract nor an entry in
NOT_SITE_AGENT_OBJECT_TYPES.

--update also writes docs/site-agent-events.md, which waldur-docs publishes in
its developer guide.

When modifying an existing message (seen from the agent that receives it):
  - Adding a field, optional or required → non-breaking (old agents ignore it)
  - Making an optional field required → non-breaking (it is just always there)
  - Removing an enum value, or declaring the values of a field → non-breaking
  - Sending the message on another object type as well → non-breaking
  - Removing fields, or making them optional or nullable → BREAKING
  - Changing field types → BREAKING
  - Adding an enum value → BREAKING (old agents may not handle it)
  - Removing an object type from object_types → BREAKING (agents subscribe by
    type, so they stop receiving the message)

The envelope (ENVELOPE_FIELDS) is compared by the same rules as a message.

The merge request pipeline compares contracts/asyncapi.yaml with the target
branch's (job "Check STOMP contract compatibility"). A breaking change fails it
unless the major part of CONTRACTS_VERSION is bumped in the same change; raise
MINIMUM_SITE_AGENT_VERSION too when agents must upgrade to handle it.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# The AsyncAPI document generated from these contracts. It is the snapshot the
# breaking-change check compares against, and the published description of the
# messages for consumers and tooling.
ASYNCAPI_PATH = (
    Path(__file__).parent.parent.parent.parent / "contracts" / "asyncapi.yaml"
)
# A reference page generated from the same declarations. waldur-docs pulls
# mastermind's docs/ into its developer guide.
DOCS_PATH = Path(__file__).parent.parent.parent.parent / "docs" / "site-agent-events.md"
ASYNCAPI_VERSION = "3.0.0"
# Version of the contract document itself, bumped on breaking changes.
CONTRACTS_VERSION = "1.0.0"


@dataclass
class MessageField:
    name: str
    type: str  # "string", "integer", "boolean", "object", "array"
    required: bool = True
    nullable: bool = False
    description: str = ""
    items_type: str = ""  # for arrays
    # The only values the field takes, when it is a closed set
    enum: list[str] = field(default_factory=list)
    added_in: str = ""  # version when field was added


@dataclass
class MessageContract:
    name: str
    description: str
    # "path:function" of every function, relative to waldur_mastermind/, that
    # builds and publishes this message
    producers: list[str] = field(default_factory=list)
    # ObservableObjectType values whose events carry this message
    object_types: list[str] = field(default_factory=list)
    fields: list[MessageField] = field(default_factory=list)
    added_in: str = ""  # version when message type was added
    notes: str = ""


# ============================================================
# Envelope — added to every message by the dispatcher
# ============================================================

# Fields the dispatcher adds around every payload. They are not repeated in each
# contract; a message may carry them on top of its declared fields. "Required"
# holds for unified consumer queues only: the legacy per-offering subscription
# path sends offering_uuid alone (see the module docstring).
ENVELOPE_FIELDS = [
    MessageField(
        "object_type", "string", description="ObservableObjectType of the event"
    ),
    MessageField(
        "schema_version", "integer", description="Version of the envelope format"
    ),
    MessageField(
        "emitted_at",
        "string",
        description="ISO 8601 time the event was emitted, for discarding stale updates",
    ),
    MessageField(
        "event_type",
        "string",
        required=False,
        description="Event type, when the event comes from the event log",
    ),
    MessageField(
        "offering_uuid",
        "string",
        required=False,
        description="UUID of the offering; added to every offering-scoped event",
    ),
]

# ============================================================
# Context added by the payload enrichers (marketplace/utils.py)
# ============================================================

# Added by _enrich_order_payload when the order still exists.
ORDER_CONTEXT_FIELDS = [
    MessageField("order_type", "string", required=False, description="Order type"),
    MessageField(
        "resource_uuid", "string", required=False, description="UUID of the resource"
    ),
    MessageField(
        "resource_backend_id",
        "string",
        required=False,
        description="Backend identifier of the resource",
    ),
    MessageField(
        "resource_name", "string", required=False, description="Name of the resource"
    ),
    MessageField(
        "project_uuid", "string", required=False, description="UUID of the project"
    ),
    MessageField(
        "project_name", "string", required=False, description="Name of the project"
    ),
    MessageField(
        "attributes",
        "object",
        required=False,
        description="Order attributes with secrets removed",
    ),
    MessageField("limits", "object", required=False, description="Resource limits"),
    MessageField(
        "plan_uuid",
        "string",
        required=False,
        description="UUID of the resource's plan, when it has one",
    ),
]

# Added by _enrich_resource_payload when the resource still exists.
RESOURCE_CONTEXT_FIELDS = [
    MessageField(
        "resource_name", "string", required=False, description="Name of the resource"
    ),
    MessageField(
        "resource_state",
        "string",
        required=False,
        description="Display name of the resource state",
    ),
    MessageField(
        "project_uuid", "string", required=False, description="UUID of the project"
    ),
    MessageField(
        "project_name", "string", required=False, description="Name of the project"
    ),
    MessageField("limits", "object", required=False, description="Resource limits"),
]

# Added by _enrich_user_role_payload when the user still exists.
USER_CONTEXT_FIELDS = [
    MessageField(
        "user_email", "string", required=False, description="Email of the user"
    ),
    MessageField(
        "user_full_name", "string", required=False, description="Full name of the user"
    ),
]


def _offering_user_fields(*extra):
    return [
        MessageField(
            "offering_user_uuid", "string", description="UUID of the offering user"
        ),
        MessageField("user_uuid", "string", description="UUID of the Waldur user"),
        MessageField("username", "string", description="Username of the offering user"),
        *extra,
    ]


def _offering_user_state_fields():
    return [
        MessageField(
            "state", "string", description="Display name of the offering user state"
        ),
        MessageField(
            "runtime_state",
            "string",
            description="Runtime state reported by the backend",
        ),
    ]


# ============================================================
# Contract declarations — one per STOMP message shape
# ============================================================

CONTRACTS = [
    MessageContract(
        name="OrderMessage",
        description="Sent on every state transition of an order.",
        producers=[
            "marketplace/handlers.py:send_order_state_change_to_message_queue",
        ],
        object_types=["order"],
        fields=[
            MessageField("order_uuid", "string", description="UUID of the order"),
            MessageField(
                "order_state", "string", description="Display name of the order state"
            ),
            *ORDER_CONTEXT_FIELDS,
        ],
    ),
    MessageContract(
        name="PendingOrderMessage",
        description="Periodic resend of pending orders (hourly task).",
        producers=[
            "marketplace_site_agent/tasks.py:send_messages_about_pending_orders",
        ],
        object_types=["order"],
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
            *ORDER_CONTEXT_FIELDS,
        ],
    ),
    MessageContract(
        name="OrderInfoMessage",
        description="Sent when the provider or the consumer sets order information "
        "for the other side.",
        producers=[
            "marketplace/tasks.py:notify_consumer_about_provider_info",
            "marketplace/tasks.py:notify_provider_about_consumer_info",
        ],
        object_types=["order"],
        fields=[
            MessageField("order_uuid", "string", description="UUID of the order"),
            MessageField(
                "action",
                "string",
                description="Action: 'provider_info_set' or 'consumer_info_set'",
                enum=["provider_info_set", "consumer_info_set"],
            ),
            *ORDER_CONTEXT_FIELDS,
        ],
    ),
    MessageContract(
        name="ResourceUpdateMessage",
        description="Sent when resource downscale, access restriction, or pause state changes.",
        producers=["marketplace_site_agent/utils.py:push_resource_update_message"],
        object_types=["resource"],
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
            *RESOURCE_CONTEXT_FIELDS,
        ],
    ),
    MessageContract(
        name="ResourceStateMessage",
        description="Sent when a resource is created and on every state transition.",
        producers=[
            "marketplace/handlers.py:send_resource_state_change_to_message_queue",
        ],
        object_types=["resource"],
        fields=[
            MessageField("resource_uuid", "string", description="UUID of the resource"),
            MessageField(
                "resource_backend_id",
                "string",
                description="Backend identifier of the resource",
            ),
            *RESOURCE_CONTEXT_FIELDS,
        ],
    ),
    MessageContract(
        name="OfferingUserMessage",
        description="Sent when an offering user's username is set or changed.",
        producers=[
            "marketplace_site_agent/handlers.py:send_offering_user_username_message",
        ],
        object_types=["offering_user"],
        fields=_offering_user_fields(
            MessageField(
                "state",
                "integer",
                description="State of the offering user, as its raw value",
            ),
            MessageField(
                "action",
                "string",
                description="Action type: username_set",
                enum=["username_set"],
            ),
            MessageField(
                "resource_backend_ids",
                "array",
                items_type="string",
                description="Backend IDs of associated resources",
            ),
        ),
    ),
    MessageContract(
        name="OfferingUserCreatedMessage",
        description="Sent when an offering user is created.",
        producers=["marketplace/handlers.py:send_offering_user_created_message"],
        object_types=["offering_user"],
        fields=_offering_user_fields(
            *_offering_user_state_fields(),
            MessageField(
                "action",
                "string",
                description="Action type: create",
                enum=["create"],
            ),
            MessageField(
                "attributes",
                "object",
                description="User profile attributes exposed to the offering",
            ),
        ),
    ),
    MessageContract(
        name="OfferingUserUpdatedMessage",
        description="Sent when tracked fields of an offering user change.",
        producers=["marketplace/handlers.py:send_offering_user_updated_message"],
        object_types=["offering_user"],
        fields=_offering_user_fields(
            *_offering_user_state_fields(),
            MessageField(
                "changed_fields",
                "array",
                items_type="string",
                description="Fields that changed",
            ),
            MessageField(
                "action",
                "string",
                description="Action type: update",
                enum=["update"],
            ),
        ),
    ),
    MessageContract(
        name="OfferingUserDeletedMessage",
        description="Sent when an offering user is deleted.",
        producers=["marketplace/handlers.py:send_offering_user_deleted_message"],
        object_types=["offering_user"],
        fields=_offering_user_fields(
            MessageField(
                "action",
                "string",
                description="Action type: delete",
                enum=["delete"],
            ),
        ),
    ),
    MessageContract(
        name="OfferingUserAttributeUpdateMessage",
        description="Sent for each offering of a user when profile attributes "
        "exposed to that offering change.",
        producers=["marketplace/handlers.py:send_user_attribute_update_message"],
        object_types=["offering_user"],
        fields=_offering_user_fields(
            MessageField(
                "action",
                "string",
                description="Action type: attribute_update",
                enum=["attribute_update"],
            ),
            MessageField(
                "changed_attributes",
                "array",
                items_type="string",
                description="Exposed profile fields that changed",
            ),
            MessageField(
                "attributes",
                "object",
                description="User profile attributes exposed to the offering",
            ),
        ),
    ),
    MessageContract(
        name="OfferingUserAccountStatusUpdateMessage",
        description="Sent for each offering of a user when the user's Waldur "
        "account is activated or deactivated.",
        producers=["marketplace/handlers.py:send_user_account_status_message"],
        object_types=["offering_user"],
        fields=_offering_user_fields(
            MessageField(
                "action",
                "string",
                description="Action type: account_status_update",
                enum=["account_status_update"],
            ),
            MessageField(
                "user_is_active",
                "boolean",
                description="Whether the Waldur user account is active",
            ),
        ),
    ),
    MessageContract(
        name="UserRoleMessage",
        description="Sent when a user role is granted or revoked in a project.",
        producers=["marketplace_site_agent/handlers.py:process_role_changed"],
        object_types=["user_role"],
        fields=[
            MessageField("user_uuid", "string", description="UUID of the user"),
            MessageField("user_username", "string", description="Username of the user"),
            MessageField("project_uuid", "string", description="UUID of the project"),
            MessageField("project_name", "string", description="Name of the project"),
            MessageField("role_name", "string", description="Name of the role"),
            MessageField(
                "granted", "boolean", description="True if granted, False if revoked"
            ),
            *USER_CONTEXT_FIELDS,
        ],
    ),
    MessageContract(
        name="UserRoleSyncMessage",
        description="Sent to trigger a user-role sync for a project, or for one "
        "resource when resource_uuid is present.",
        producers=[
            "marketplace_site_agent/utils.py:push_user_role_sync_message",
            "marketplace_site_agent/utils.py:push_resource_user_role_sync_message",
        ],
        object_types=["user_role"],
        fields=[
            MessageField("project_uuid", "string", description="UUID of the project"),
            MessageField("project_name", "string", description="Name of the project"),
            MessageField(
                "resource_uuid",
                "string",
                required=False,
                description="UUID of the resource to re-sync; absent for a "
                "project-wide sync",
            ),
        ],
    ),
    MessageContract(
        name="AccountMessage",
        description="Sent when a service or course account is created or deleted.",
        producers=["marketplace_site_agent/handlers.py:send_account_message"],
        object_types=["service_account", "course_account"],
        fields=[
            MessageField("account_uuid", "string", description="UUID of the account"),
            MessageField(
                "account_username", "string", description="Username of the account"
            ),
            MessageField(
                "scope_type",
                "string",
                description="Scope type (always 'project')",
                enum=["project"],
            ),
            MessageField("project_uuid", "string", description="UUID of the project"),
            MessageField("project_name", "string", description="Name of the project"),
            MessageField(
                "action",
                "string",
                description="Action: 'create' or 'delete'",
                enum=["create", "delete"],
            ),
        ],
    ),
    MessageContract(
        name="PeriodicLimitsMessage",
        description="Sent when SLURM policy periodic settings need to be applied.",
        producers=[
            "policy/models.py:SlurmPeriodicUsagePolicy._send_settings_to_site_agent"
        ],
        object_types=["resource_periodic_limits"],
        fields=[
            MessageField("resource_uuid", "string", description="UUID of the resource"),
            MessageField(
                "backend_id",
                "string",
                description="Backend resource identifier (SLURM account name)",
            ),
            MessageField("offering_uuid", "string", description="UUID of the offering"),
            MessageField("policy_uuid", "string", description="UUID of the policy"),
            MessageField(
                "action",
                "string",
                description="Action: 'apply_periodic_settings'",
                enum=["apply_periodic_settings"],
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
    MessageContract(
        name="BackendResourceRequestMessage",
        description="Asks the agent to list the backend resources that can be "
        "imported into the offering.",
        producers=["marketplace/utils.py:publish_backend_resource_request"],
        object_types=["importable_resources"],
        fields=[
            MessageField(
                "backend_resource_request_uuid",
                "string",
                description="UUID of the backend resource request to fulfill",
            ),
        ],
    ),
    MessageContract(
        name="OfferingResourcesSyncMessage",
        description="Asks the agent to reconcile every resource of the offering.",
        producers=["marketplace/utils.py:publish_offering_resources_sync_request"],
        object_types=["offering_resources_sync"],
        fields=[
            MessageField(
                "requested_by_user_uuid",
                "string",
                description="UUID of the user who requested the synchronization",
            ),
        ],
    ),
    MessageContract(
        name="ApiKeyCommandMessage",
        description="A command on one of a resource's API keys: create, rotate, "
        "pause, resume, update or delete. Carried on the "
        "resource_api_key_rotation observable type, which is named for the "
        "first command and kept so subscriptions do not change.",
        producers=["marketplace/utils.py:publish_api_key_event"],
        object_types=["resource_api_key_rotation"],
        fields=[
            MessageField(
                "action",
                "string",
                description="One of ResourceApiKeyActions",
                enum=["create", "rotate", "pause", "resume", "delete", "update"],
            ),
            MessageField("resource_uuid", "string", description="UUID of the resource"),
            MessageField(
                "resource_backend_id",
                "string",
                description="Backend identifier of the resource",
            ),
            MessageField("api_key_uuid", "string", description="UUID of the API key"),
            MessageField(
                "client_id",
                "string",
                description="Backend client id of the key; blank on a key being created",
            ),
            MessageField(
                "limits",
                "object",
                required=False,
                nullable=True,
                description="Monthly limits per component, null when the key has "
                "none; only on commands that carry the key's settings",
            ),
            MessageField(
                "spending_limits",
                "object",
                required=False,
                nullable=True,
                description="Spending limits for every period, null when the key "
                "has none; only on commands that carry the key's settings",
            ),
            MessageField(
                "allowed_models",
                "array",
                required=False,
                items_type="string",
                description="Models the key may call (['*'] for all, [] for none): "
                "the key's own list narrowed by its resource's model selection. "
                "Only on commands that carry the key's settings; absent from "
                "Waldur versions older than ['*'], where absence allows every model",
            ),
        ],
    ),
    MessageContract(
        name="ProjectGroupMessage",
        description="A provider project group was created, updated or deleted, or "
        "project groups were switched on or off for the provider.",
        producers=[
            "marketplace/handlers.py:send_project_group_saved_message",
            "marketplace/handlers.py:send_project_group_deleted_message",
            "marketplace/handlers.py:send_project_groups_switch_message",
            "marketplace/project_groups.py:allocate_gid",
        ],
        object_types=["service_provider_project_group"],
        fields=[
            MessageField(
                "action",
                "string",
                description="Action: 'create' (the group got its first GID), "
                "'update' (its name or GID changed), 'delete', or 'switch' "
                "(project groups switched on or off for the provider)",
                enum=["create", "update", "delete", "switch"],
            ),
            MessageField(
                "service_provider_uuid",
                "string",
                description="UUID of the service provider",
            ),
            MessageField(
                "customer_uuid",
                "string",
                description="UUID of the provider's organization",
            ),
            MessageField(
                "project_group_uuid",
                "string",
                required=False,
                description="UUID of the group; absent on 'switch'",
            ),
            MessageField(
                "project_uuid",
                "string",
                required=False,
                nullable=True,
                description="UUID of the group's project if it still exists; "
                "absent on 'switch'",
            ),
            MessageField(
                "name",
                "string",
                required=False,
                description="Group name; absent on 'switch'",
            ),
            MessageField(
                "gid",
                "integer",
                required=False,
                nullable=True,
                description="Group GID; absent on 'switch'",
            ),
            MessageField(
                "changed_fields",
                "array",
                required=False,
                items_type="string",
                description="Fields that changed; only on 'update'",
            ),
            MessageField(
                "project_groups_enabled",
                "boolean",
                required=False,
                description="The new setting; only on 'switch'",
            ),
        ],
    ),
]

# ObservableObjectType values whose events are not meant for the site agent,
# with the reason. Every other type must be covered by a contract above, so a
# new event type fails test_contracts.py until someone decides which it is.
NOT_SITE_AGENT_OBJECT_TYPES = {
    "resource_end_date_change_request": "Read by an external approval system, "
    "which answers through the request's approve/reject actions.",
    "user_profile": "User-centric event for global IdM/IGA consumers.",
    "user_ssh_key": "User-centric event for global IdM/IGA consumers.",
    "user_lifecycle": "User-centric event for global IdM/IGA consumers.",
    "service_provider_account": "Provider-scoped account event for consumers "
    "other than the agent; the agent follows offering_user, which is still "
    "emitted alongside it.",
    "call": "Proposal-domain event for consumers bound to a call.",
    "proposal": "Proposal-domain event for consumers bound to a call or proposal.",
}

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
    if not ASYNCAPI_PATH.exists():
        return {"error": "No snapshot found"}

    old = asyncapi_to_schema(load_asyncapi())
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


def _fields_to_schema(fields):
    properties = {}
    required = []
    for f in fields:
        prop = {"type": f.type, "description": f.description}
        if f.nullable:
            prop["nullable"] = True
        if f.type == "array" and f.items_type:
            prop["items"] = {"type": f.items_type}
        if f.enum:
            prop["enum"] = list(f.enum)
        properties[f.name] = prop
        if f.required:
            required.append(f.name)
    return {"required": sorted(required), "properties": properties}


def contracts_to_schema(contracts):
    """Convert contract declarations to a JSON Schema document."""
    messages = {}
    for contract in contracts:
        messages[contract.name] = {
            "type": "object",
            "title": contract.name,
            "description": contract.description,
            "producers": contract.producers,
            "object_types": contract.object_types,
            **_fields_to_schema(contract.fields),
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Waldur STOMP Message Contracts",
        "description": "Contracts for mastermind → site-agent STOMP messages. "
        "Auto-generated from contracts.py declarations.",
        "version": CONTRACTS_VERSION,
        "envelope": {"type": "object", **_fields_to_schema(ENVELOPE_FIELDS)},
        "messages": messages,
    }


# ============================================================
# AsyncAPI 3.0 document
# ============================================================

CONSUMER_CHANNEL = "consumerQueue"
SEND_OPERATION = "sendSiteAgentEvents"


def _field_to_json_schema(f):
    schema = {
        "type": [f.type, "null"] if f.nullable else f.type,
        "description": f.description,
    }
    if f.type == "array" and f.items_type:
        schema["items"] = {"type": f.items_type}
    if f.enum:
        # A nullable field's enum must admit null too, or null fails validation.
        schema["enum"] = [*f.enum, None] if f.nullable else list(f.enum)
    return schema


def _object_schema(fields, extra_properties=None):
    properties = {f.name: _field_to_json_schema(f) for f in fields}
    for name, schema in (extra_properties or {}).items():
        properties.setdefault(name, schema)
    schema = {"type": "object"}
    required = [f.name for f in fields if f.required]
    if required:
        schema["required"] = required
    schema["properties"] = properties
    return schema


def _message(contract):
    object_type = {
        "type": "string",
        "enum": list(contract.object_types),
        "description": "ObservableObjectType this message is delivered for",
    }
    return {
        "name": contract.name,
        "title": contract.name,
        "summary": contract.description,
        "contentType": "application/json",
        "payload": {
            "allOf": [
                {"$ref": "#/components/schemas/Envelope"},
                _object_schema(contract.fields, {"object_type": object_type}),
            ]
        },
        "x-waldur-object-types": list(contract.object_types),
        "x-waldur-producers": list(contract.producers),
    }


def contracts_to_asyncapi(contracts):
    """The AsyncAPI 3.0 document describing ``contracts``."""
    names = [contract.name for contract in contracts]
    return {
        "asyncapi": ASYNCAPI_VERSION,
        "info": {
            "title": "Waldur site agent events",
            "version": CONTRACTS_VERSION,
            "description": (
                "Messages mastermind publishes to site agents. Generated from "
                "waldur_mastermind/marketplace_site_agent/contracts.py; do not "
                "edit by hand. Run `uv run python -m "
                "waldur_mastermind.marketplace_site_agent.contracts --update` "
                "after changing a contract.\n\n"
                "Only the unified consumer queue is described; webhook "
                "consumers receive the same messages. The deprecated "
                "per-subscription topics are not modeled: they carry the "
                "message's own fields plus offering_uuid, without the rest of "
                "the envelope (object_type, schema_version, emitted_at) and "
                "without the context fields the payload enrichers add."
            ),
            "x-minimum-site-agent-version": MINIMUM_SITE_AGENT_VERSION,
            "x-not-site-agent-object-types": dict(NOT_SITE_AGENT_OBJECT_TYPES),
        },
        "defaultContentType": "application/json",
        "servers": {
            "rabbitmq": {
                "host": "{host}",
                "protocol": "stomp",
                "description": (
                    "RabbitMQ Web-STOMP endpoint of the Waldur deployment. Each "
                    "consumer owner has a vhost named by their user UUID; agents "
                    "subscribe to /amq/queue/<queue> with client-individual "
                    "acknowledgment."
                ),
                "variables": {
                    "host": {
                        "description": "Host and port of the RabbitMQ Web-STOMP "
                        "endpoint"
                    }
                },
            }
        },
        "channels": {
            CONSUMER_CHANNEL: {
                "address": "consumer_{consumerUuid}",
                "title": "Consumer queue",
                "description": (
                    "The queue of one registered event consumer. It receives "
                    "every event its scope bindings and object types admit."
                ),
                "parameters": {
                    "consumerUuid": {"description": "UUID of the event consumer"}
                },
                "messages": {
                    name: {"$ref": f"#/components/messages/{name}"} for name in names
                },
            }
        },
        "operations": {
            SEND_OPERATION: {
                "action": "send",
                "title": "Publish site agent events",
                "channel": {"$ref": f"#/channels/{CONSUMER_CHANNEL}"},
                "messages": [
                    {"$ref": f"#/channels/{CONSUMER_CHANNEL}/messages/{name}"}
                    for name in names
                ],
            }
        },
        "components": {
            "schemas": {
                "Envelope": {
                    **_object_schema(ENVELOPE_FIELDS),
                    "description": "Fields the dispatcher adds to every message",
                }
            },
            "messages": {contract.name: _message(contract) for contract in contracts},
        },
    }


def dump_asyncapi(document) -> str:
    return yaml.safe_dump(
        document,
        sort_keys=False,
        allow_unicode=True,
        width=88,
        default_flow_style=False,
    )


def load_asyncapi(path=ASYNCAPI_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def _comparison_fields(body, skip=()):
    """{"required", "properties"} of one object schema, in the comparison form."""
    properties = {}
    for field_name, schema in body.get("properties", {}).items():
        if field_name in skip:
            continue
        types = schema["type"]
        types = types if isinstance(types, list) else [types]
        prop = {
            "type": next(t for t in types if t != "null"),
            "description": schema.get("description", ""),
        }
        if "null" in types:
            prop["nullable"] = True
        if "items" in schema:
            prop["items"] = schema["items"]
        if "enum" in schema:
            prop["enum"] = [value for value in schema["enum"] if value is not None]
        properties[field_name] = prop
    return {"required": sorted(body.get("required", [])), "properties": properties}


def asyncapi_to_schema(document):
    """The comparison form used by compare_schemas, read from an AsyncAPI document."""
    components = document.get("components", {})
    messages = {}
    for name, message in components.get("messages", {}).items():
        body = message["payload"]["allOf"][1]
        # The object types the payload pins, which is what agents receive.
        pinned = body.get("properties", {}).get("object_type", {}).get("enum")
        messages[name] = {
            # object_type is compared as the message's object types below.
            **_comparison_fields(body, skip=("object_type",)),
            "object_types": list(pinned or message.get("x-waldur-object-types", [])),
        }
    schema = {"messages": messages}
    envelope = components.get("schemas", {}).get("Envelope")
    if envelope is not None:
        schema["envelope"] = _comparison_fields(envelope)
    return schema


def _major(version: str) -> int:
    return int(str(version).split(".")[0])


def check_compatibility(base_document, head_document):
    """Compare two AsyncAPI documents the way CI does for a merge request.

    A breaking change is acceptable only together with a bump of the major part
    of ``info.version`` (CONTRACTS_VERSION), which is what tells consumers to
    expect it.
    """
    changes = compare_schemas(
        asyncapi_to_schema(base_document), asyncapi_to_schema(head_document)
    )
    breaking = [c for c in changes if c["breaking"]]
    base_version = base_document["info"]["version"]
    head_version = head_document["info"]["version"]
    bumped = _major(head_version) > _major(base_version)
    if not changes:
        summary = "No STOMP contract changes."
    elif not breaking:
        summary = f"{len(changes)} non-breaking STOMP contract change(s)."
    elif bumped:
        summary = (
            f"{len(breaking)} breaking STOMP contract change(s), released as "
            f"contracts version {head_version} (was {base_version})."
        )
    else:
        summary = (
            f"{len(breaking)} breaking STOMP contract change(s) without a major "
            f"version bump (contracts version {head_version}, was {base_version}). "
            "Make the change compatible, or bump the major part of "
            "CONTRACTS_VERSION in marketplace_site_agent/contracts.py, raise "
            "MINIMUM_SITE_AGENT_VERSION if agents must upgrade to handle it, and "
            "regenerate with --update."
        )
    return {
        "changes": changes,
        "compatible": not breaking or bumped,
        "summary": summary,
    }


# ============================================================
# Markdown reference
# ============================================================


def _md(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _md_type(f) -> str:
    text = f"array of {f.items_type}" if f.type == "array" and f.items_type else f.type
    if f.nullable:
        text += " or null"
    if f.enum:
        text += ": " + ", ".join(f"`{value}`" for value in f.enum)
    return text


def _md_fields_table(fields) -> list[str]:
    lines = ["| Field | Type | Required | Description |", "| --- | --- | --- | --- |"]
    lines += [
        f"| `{f.name}` | {_md_type(f)} | {'yes' if f.required else 'no'} | "
        f"{_md(f.description)} |"
        for f in fields
    ]
    return lines


def contracts_to_markdown(contracts) -> str:
    """The site agent event reference page, generated from ``contracts``."""
    lines = [
        "# Site agent events",
        "",
        "<!-- Generated by `uv run python -m waldur_mastermind.marketplace_site_agent"
        ".contracts --update` from `src/waldur_mastermind/marketplace_site_agent/"
        "contracts.py`. Do not edit by hand. -->",
        "",
        "Mastermind tells site agents about changes they must act on by "
        "publishing STOMP messages. This page lists every message, its fields and "
        "the code that sends it. The same contracts are published as an AsyncAPI "
        f"{ASYNCAPI_VERSION} document, `contracts/asyncapi.yaml` in the "
        "waldur-mastermind repository, which standard AsyncAPI tooling can "
        "validate and generate code from.",
        "",
        f"- Contracts version: `{CONTRACTS_VERSION}`. Its major part changes on "
        "every breaking change.",
        f"- Minimum site agent version: `{MINIMUM_SITE_AGENT_VERSION}`.",
        "",
        "## Transport",
        "",
        "Messages are JSON objects sent over RabbitMQ Web-STOMP. Each registered "
        "event consumer has one queue, `consumer_<consumer UUID>`, in the vhost "
        "named by its owner's user UUID. The agent subscribes to "
        "`/amq/queue/<queue>` with `client-individual` acknowledgment, so a "
        "message it does not acknowledge is delivered again. Delivery is "
        "at-least-once and unordered across events: use `emitted_at` to discard "
        "an update older than one already applied.",
        "",
        "A consumer receives the messages that its scope bindings and object "
        "types admit. Several messages share an object type, so tell them apart "
        "by `action` or by the fields present.",
        "",
        "Site agents before 1.0.7 subscribe through the deprecated "
        "per-subscription topics instead. Those carry each message's own fields "
        "plus `offering_uuid` only: no `object_type`, `schema_version` or "
        "`emitted_at`, and none of the context fields marked optional below that "
        "the payload enrichers add.",
        "",
        "## Envelope",
        "",
        "The dispatcher adds these fields to every message.",
        "",
        *_md_fields_table(ENVELOPE_FIELDS),
        "",
        "## Messages",
        "",
    ]
    by_type: dict[str, list] = {}
    for contract in contracts:
        by_type.setdefault(", ".join(contract.object_types), []).append(contract)
    for object_types, group in by_type.items():
        lines += [f"### Object type `{object_types.replace(', ', '`, `')}`", ""]
        for contract in group:
            lines += [f"#### {contract.name}", "", _md(contract.description), ""]
            lines += ["Sent by:", ""]
            lines += [f"- `{producer}`" for producer in contract.producers]
            lines += ["", *_md_fields_table(contract.fields), ""]
    lines += [
        "## Event types not delivered to site agents",
        "",
        "The same event stream carries these types for other consumers.",
        "",
        "| Object type | Consumer |",
        "| --- | --- |",
        *[
            f"| `{object_type}` | {_md(reason)} |"
            for object_type, reason in NOT_SITE_AGENT_OBJECT_TYPES.items()
        ],
        "",
    ]
    return "\n".join(lines)


def _change(kind, breaking, message, description, **detail):
    return {
        "type": kind,
        "breaking": breaking,
        "message": message,
        **detail,
        "description": description,
    }


def _compare_enums(name, field_name, old_prop, new_prop):
    old_enum, new_enum = old_prop.get("enum"), new_prop.get("enum")
    if old_enum is None and new_enum is not None:
        return [
            _change(
                "enum_added",
                False,
                name,
                f"Field '{field_name}' in '{name}' now declares its values: "
                f"{', '.join(new_enum)}",
                field=field_name,
            )
        ]
    if old_enum is not None and new_enum is None:
        return [
            _change(
                "enum_removed",
                True,
                name,
                f"Field '{field_name}' in '{name}' no longer has a closed set of "
                "values",
                field=field_name,
            )
        ]
    if old_enum is None:
        return []
    added = [
        _change(
            "enum_value_added",
            True,
            name,
            f"Value '{value}' added to '{field_name}' in '{name}'",
            field=field_name,
            value=value,
        )
        for value in new_enum
        if value not in old_enum
    ]
    removed = [
        _change(
            "enum_value_removed",
            False,
            name,
            f"Value '{value}' removed from '{field_name}' in '{name}'",
            field=field_name,
            value=value,
        )
        for value in old_enum
        if value not in new_enum
    ]
    return added + removed


def _compare_items(name, field_name, old_prop, new_prop):
    if old_prop.get("type") != "array" or new_prop.get("type") != "array":
        return []
    old_items = old_prop.get("items")
    new_items = new_prop.get("items")
    old_type = old_items.get("type") if isinstance(old_items, dict) else None
    new_type = new_items.get("type") if isinstance(new_items, dict) else None
    if old_type == new_type:
        return []
    if old_type is None and new_type is not None:
        return [
            _change(
                "array_items_typed",
                False,
                name,
                f"Field '{field_name}' in '{name}' now declares item type '{new_type}'",
                field=field_name,
                to=new_type,
            )
        ]
    if old_type is not None and new_type is None:
        return [
            _change(
                "array_items_untyped",
                True,
                name,
                f"Field '{field_name}' in '{name}' no longer declares item type '{old_type}'",
                field=field_name,
                **{"from": old_type},
            )
        ]
    return [
        _change(
            "array_items_type_changed",
            True,
            name,
            f"Items of '{field_name}' in '{name}' changed type from "
            f"'{old_type}' to '{new_type}'",
            field=field_name,
            **{"from": old_type, "to": new_type},
        )
    ]


def _compare_fields(name, old_msg, new_msg):
    """Field changes between two versions of one message (or the envelope).

    Seen from the agent that receives the message: data it can ignore is never
    breaking, while a field that may now be absent, null, of another type, or
    hold a value it was never told about can break a handler written against
    the old contract.
    """
    changes = []
    old_props = old_msg.get("properties", {})
    new_props = new_msg.get("properties", {})
    old_req = set(old_msg.get("required", []))
    new_req = set(new_msg.get("required", []))

    for field_name in old_props:
        if field_name not in new_props:
            changes.append(
                _change(
                    "field_removed",
                    True,
                    name,
                    f"Field '{field_name}' removed from '{name}'",
                    field=field_name,
                )
            )

    for field_name in new_props:
        if field_name not in old_props:
            kind = "Required" if field_name in new_req else "Optional"
            changes.append(
                _change(
                    "field_added",
                    False,
                    name,
                    f"{kind} field '{field_name}' added to '{name}'",
                    field=field_name,
                )
            )

    for field_name in old_props:
        if field_name not in new_props:
            continue
        old_prop, new_prop = old_props[field_name], new_props[field_name]
        if old_prop.get("type") != new_prop.get("type"):
            changes.append(
                _change(
                    "field_type_changed",
                    True,
                    name,
                    f"Field '{field_name}' in '{name}' changed type from "
                    f"'{old_prop.get('type')}' to '{new_prop.get('type')}'",
                    field=field_name,
                    **{"from": old_prop.get("type"), "to": new_prop.get("type")},
                )
            )
        if field_name in new_req and field_name not in old_req:
            changes.append(
                _change(
                    "field_became_required",
                    False,
                    name,
                    f"Field '{field_name}' in '{name}' became required",
                    field=field_name,
                )
            )
        if field_name in old_req and field_name not in new_req:
            changes.append(
                _change(
                    "field_became_optional",
                    True,
                    name,
                    f"Field '{field_name}' in '{name}' became optional",
                    field=field_name,
                )
            )
        if new_prop.get("nullable") and not old_prop.get("nullable"):
            changes.append(
                _change(
                    "field_became_nullable",
                    True,
                    name,
                    f"Field '{field_name}' in '{name}' became nullable",
                    field=field_name,
                )
            )
        changes.extend(_compare_items(name, field_name, old_prop, new_prop))
        changes.extend(_compare_enums(name, field_name, old_prop, new_prop))

    return changes


def _compare_object_types(name, old_msg, new_msg):
    """Object type changes of one message. Agents subscribe by object type, so
    a message that stops travelling on a type no longer reaches them."""
    # A document written before object types were recorded has nothing to compare.
    if "object_types" not in old_msg:
        return []
    old_types = set(old_msg["object_types"])
    new_types = set(new_msg.get("object_types", []))
    removed = [
        _change(
            "object_type_removed",
            True,
            name,
            f"'{name}' is no longer sent on object type '{object_type}'",
            object_type=object_type,
        )
        for object_type in sorted(old_types - new_types)
    ]
    added = [
        _change(
            "object_type_added",
            False,
            name,
            f"'{name}' is now also sent on object type '{object_type}'",
            object_type=object_type,
        )
        for object_type in sorted(new_types - old_types)
    ]
    return removed + added


def compare_schemas(old, new):
    """Compare two contract schemas and return a list of changes."""
    changes = []
    old_msgs = old.get("messages", {})
    new_msgs = new.get("messages", {})

    for name in old_msgs:
        if name not in new_msgs:
            changes.append(
                _change(
                    "message_removed", True, name, f"Message type '{name}' was removed"
                )
            )

    for name in new_msgs:
        if name not in old_msgs:
            changes.append(
                _change(
                    "message_added", False, name, f"New message type '{name}' added"
                )
            )

    for name in old_msgs:
        if name not in new_msgs:
            continue
        changes.extend(_compare_fields(name, old_msgs[name], new_msgs[name]))
        changes.extend(_compare_object_types(name, old_msgs[name], new_msgs[name]))

    # A document written before the envelope was declared has nothing to compare.
    if "envelope" in old:
        changes.extend(
            _compare_fields("envelope", old["envelope"], new.get("envelope", {}))
        )

    return changes


def main():
    parser = argparse.ArgumentParser(description="STOMP message contract tool")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Regenerate contracts/asyncapi.yaml and docs/site-agent-events.md",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report contract changes; with --base, against another document",
    )
    parser.add_argument(
        "--base",
        type=Path,
        help="AsyncAPI document to compare with, e.g. the target branch's. A "
        "breaking change then fails unless info.version's major part was bumped",
    )
    parser.add_argument(
        "--impact",
        action="store_true",
        help="Check impact on registered agents (requires Django)",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    schema = contracts_to_schema(CONTRACTS)

    if args.update:
        ASYNCAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
        ASYNCAPI_PATH.write_text(dump_asyncapi(contracts_to_asyncapi(CONTRACTS)))
        print(f"Updated: {ASYNCAPI_PATH}")
        DOCS_PATH.write_text(contracts_to_markdown(CONTRACTS))
        print(f"Updated: {DOCS_PATH}")
        return

    if args.check:
        if not ASYNCAPI_PATH.exists():
            print(f"No snapshot at {ASYNCAPI_PATH}. Run with --update first.")
            sys.exit(1)
        if args.base:
            result = check_compatibility(load_asyncapi(args.base), load_asyncapi())
            print(result["summary"])
            for c in result["changes"]:
                print(f"  {'✗' if c['breaking'] else '✓'} {c['description']}")
            sys.exit(0 if result["compatible"] else 1)
        old = asyncapi_to_schema(load_asyncapi())
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

    # Default: print the AsyncAPI document to stdout
    print(dump_asyncapi(contracts_to_asyncapi(CONTRACTS)), end="")


if __name__ == "__main__":
    main()
