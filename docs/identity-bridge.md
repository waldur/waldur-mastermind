# Identity Bridge

## Overview

The Identity Bridge enables Infrastructure Service Domains (ISDs) to **push** user attributes directly to Waldur, complementing the existing pull-based remote-eduteams flow. When Waldur participates in multiple ISDs (e.g., Puhuri, EOSC, EFP), the same user (same CUID) may arrive through different ISDs with different attribute sets. The Identity Bridge tracks which source provided which attribute and prevents one source's data revocation from wiping another source's data.

```mermaid
flowchart TB
    subgraph MyAccessID["MyAccessID Proxy (GÉANT)"]
        ISD_P["Puhuri"]
        ISD_O["EOSC"]
        ISD_E["EFP"]
    end

    subgraph Waldur
        RE["RemoteEduteamsView\n(pull-based)"]
        IB["IdentityBridgeView\n(push-based)"]
        HELPER["update_user_attributes_from_source()"]
        USER["User Model\n+ attribute_sources\n+ active_isds\n+ managed_isds"]
    end

    ISD_P -->|GET userinfo/cuid| RE
    ISD_O -->|POST /api/identity-bridge/| IB
    ISD_E -->|POST /api/identity-bridge/| IB

    RE --> HELPER
    IB --> HELPER
    HELPER --> USER
```

### Key Capabilities

- **Per-attribute source tracking**: Know which ISD provided each attribute and when
- **Preserve-other-sources policy**: One ISD cannot accidentally clear another ISD's data
- **Multi-ISD lifecycle**: Users are only deactivated when removed from all ISDs
- **ISD-scoped identity managers**: Each manager can only push for their assigned ISDs
- **Backward compatible**: Existing pull-based remote-eduteams flow continues to work

### AARC Alignment

| Standard | Relevance |
|----------|-----------|
| AARC-G003 (Attribute Aggregation) | Source attribution per attribute prevents confusion across ISDs |
| AARC-G056 (Attribute Profile) | Standardized attribute names across ISDs (same fields, different sources) |
| AARC-I101 (Verifiable Credentials) | `attribute_sources` design is forward-compatible with per-credential issuer provenance |

## Architecture

### Push vs Pull

```mermaid
flowchart LR
    subgraph Pull["Pull-based (existing)"]
        direction TB
        P1["Waldur beat task\n(every 5 min)"] --> P2["GET userinfo/&#123;cuid&#125;"]
        P2 --> P3["Update user attributes"]
    end

    subgraph Push["Push-based (Identity Bridge)"]
        direction TB
        Q1["ISD calls\nPOST /api/identity-bridge/"] --> Q2["Validate caller + source"]
        Q2 --> Q3["Update user attributes"]
    end
```

Both paths converge on the same `update_user_attributes_from_source()` helper, ensuring consistent source tracking and conflict resolution regardless of how attributes arrive.

### Source Format Convention

Sources use a structured `<type>:<name>` format:

| Format | Description | Example |
|--------|-------------|---------|
| `isd:<name>` | Infrastructure Service Domain | `isd:puhuri`, `isd:eosc` |
| `vc:<issuer>` | Future: Verifiable Credential issuer | `vc:myaccessid` |

Legacy values from existing OIDC flows are automatically converted:

| Legacy Value | Structured Format |
|-------------|------------------|
| `eduteams` | `isd:eduteams` |
| `remote-eduteams` | `isd:eduteams` |
| `tara` | `isd:tara` |
| `keycloak` | `isd:keycloak` |

## Attribute Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Unset: Field has no value

    Unset --> OwnedByPuhuri: Puhuri sends non empty value
    OwnedByPuhuri --> OwnedByEOSC: EOSC sends non empty value
    OwnedByPuhuri --> Cleared: Puhuri the owner sends empty
    OwnedByPuhuri --> OwnedByPuhuri: EOSC sends empty so preserved

    OwnedByEOSC --> OwnedByPuhuri: Puhuri sends non empty value
    OwnedByEOSC --> Cleared: EOSC the owner sends empty

    Cleared --> OwnedByPuhuri: Puhuri sends non empty value
    Cleared --> OwnedByEOSC: EOSC sends non empty value
```

**Key invariant**: Only the last writer (current owner) can clear a field. Empty values from non-owners are silently ignored.

### Tracking Format

Each attribute maps to its source and a freshness timestamp in the `attribute_sources` JSONField:

```json
{
  "email": {
    "source": "isd:eosc",
    "timestamp": "2026-02-05T12:00:00Z"
  },
  "organization": {
    "source": "isd:puhuri",
    "timestamp": "2026-02-04T09:30:00Z"
  }
}
```

Timestamps are updated even when the value hasn't changed, confirming freshness from the source.

## Multi-ISD Deactivation

```mermaid
flowchart TD
    A["Source S reports user removed\n(404 from remote API or\nPOST /api/identity-bridge/remove/)"]
    B["Remove S from user.active_isds\nClear attributes owned by S"]
    C{"Is active_isds\nnow empty?"}
    D["Deactivate user\nis_active = False"]
    E["Keep user active\n(other ISDs still reference)"]

    A --> B --> C
    C -->|Yes| D
    C -->|No| E
```

The deactivation behavior is configurable via the `FEDERATED_IDENTITY_DEACTIVATION_POLICY` setting:

| Policy | Behavior |
|--------|----------|
| `all_isds_removed` (default) | Deactivate only when `active_isds` is empty |
| `any_isd_removed` | Deactivate on first ISD removal (backward compatible) |

## Configuration

### Constance Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `FEDERATED_IDENTITY_SYNC_ENABLED` | `False` | Enable the Identity Bridge API |
| `FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES` | `["first_name", "last_name", "email", "organization", "affiliations"]` | Attributes settable via the bridge (must be a subset of `WRITABLE_USER_FIELDS`) |
| `FEDERATED_IDENTITY_DEACTIVATION_POLICY` | `"any_isd_removed"` | When to deactivate a federated user |

Configure via the Django admin Constance panel or API:

```http
PATCH /api/configuration/
Content-Type: application/json

{
  "FEDERATED_IDENTITY_SYNC_ENABLED": true,
  "FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES": [
    "first_name", "last_name", "email",
    "organization", "affiliations", "phone_number"
  ],
  "FEDERATED_IDENTITY_DEACTIVATION_POLICY": "any_isd_removed"
}
```

### Three-Way Field Intersection

The bridge does not blindly accept all fields. Effective bridge-writable fields are the **intersection** of three sets:

```mermaid
flowchart TD
    A["FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES\n(Constance — admin configurable)"]
    B["WRITABLE_USER_FIELDS\n(Code — security whitelist)"]
    C["ENABLED_USER_PROFILE_ATTRIBUTES\n(Constance — profile visibility)"]
    D["Effective bridge-writable fields"]

    A --> D
    B --> D
    C --> D
```

This ensures that:

1. Only fields the admin explicitly allows can be set via the bridge
2. Only fields the code considers safe to write are accepted
3. Only fields enabled in the user profile configuration are synced

## User Model Fields

Three new JSONFields on the User model support the Identity Bridge:

| Field | Type | Description |
|-------|------|-------------|
| `attribute_sources` | JSONField (dict) | Per-attribute source and timestamp tracking |
| `managed_isds` | JSONField (list) | ISDs this user can manage via the bridge (e.g., `["isd:puhuri"]`) |
| `active_isds` | JSONField (list) | ISDs that have asserted this user exists |

### Identity Manager Scoping

The existing `is_identity_manager` boolean is kept for backward compatibility. The new `managed_isds` field adds ISD scoping:

- `is_identity_manager=True` + empty `managed_isds` = global manager (backward compatible)
- `is_identity_manager=True` + `managed_isds=["isd:puhuri"]` = scoped to Puhuri only
- Staff users bypass ISD scope checks entirely

## API Reference

### Push Attributes

**Endpoint**: `POST /api/identity-bridge/`

Creates or updates a user based on CUID (username). The caller specifies which ISD they represent.

**Permissions**: Staff or identity manager with matching `managed_isds`

**Request**:

```json
{
  "username": "user@myaccessid.org",
  "source": "isd:puhuri",
  "first_name": "Alice",
  "last_name": "Smith",
  "email": "alice@example.com",
  "organization": "CERN"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `username` | string | yes | CUID / username of the user |
| `source` | string | yes | ISD source identifier (must match `^[a-z]+:[a-zA-Z0-9._-]+$`) |
| _attribute fields_ | various | no | Any field from `FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES` |

**Response** (200):

```json
{
  "uuid": "abc123def456...",
  "created": true,
  "updated_fields": ["email", "first_name", "last_name", "organization"]
}
```

**Error responses**:

| Status | Condition |
|--------|-----------|
| 400 | Invalid source format, disallowed fields, or deactivated user |
| 401 | Not authenticated |
| 403 | Feature disabled, insufficient permissions, or source not in `managed_isds` |

### Remove User from ISD

**Endpoint**: `POST /api/identity-bridge/remove/`

Signals that a user has been removed from an ISD. Clears attributes owned by the source and deactivates the user if no ISDs remain.

**Permissions**: Staff or identity manager with matching `managed_isds`

**Request**:

```json
{
  "username": "user@myaccessid.org",
  "source": "isd:puhuri"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `username` | string | yes | CUID / username of the user |
| `source` | string | yes | ISD source identifier to remove |

**Response** (200):

```json
{
  "uuid": "abc123def456...",
  "deactivated": true
}
```

**Error responses**:

| Status | Condition |
|--------|-----------|
| 400 | Invalid source format |
| 401 | Not authenticated |
| 403 | Feature disabled, insufficient permissions, or source not in `managed_isds` |
| 404 | User not found |

### Removal Behavior

```mermaid
sequenceDiagram
    participant ISD as ISD caller
    participant API as POST /api/identity-bridge/remove/
    participant User as User Model

    ISD->>API: {username, source: "isd:puhuri"}
    API->>API: Validate permissions + ISD scope
    API->>User: remove_user_from_isd()

    Note over User: Remove "isd:puhuri" from active_isds
    Note over User: Clear attribute_sources owned by "isd:puhuri"
    Note over User: Clear corresponding field values

    alt active_isds is now empty
        Note over User: is_active = False
        API-->>ISD: {deactivated: true}
    else other ISDs remain
        Note over User: is_active unchanged
        API-->>ISD: {deactivated: false}
    end
```

## Offering User Exposure

The `active_isds` field can be exposed to service providers via the offering user API. This allows services to know which ISDs a user is connected to without exposing full provenance data.

Enable by adding `active_isds` to `ENABLED_USER_PROFILE_ATTRIBUTES` in Constance, then configure the offering's `OfferingUserAttributeConfig` to include it.

```json
GET /api/marketplace-offering-users/

{
  "user_username": "user@myaccessid.org",
  "user_full_name": "Alice Smith",
  "user_active_isds": ["isd:eosc", "isd:puhuri", "isd:efp"]
}
```

See [Offering Users](./core-concepts/offering-users.md) and [User Profile Attributes](./user-profile-attributes.md) for configuration details.

## Audit Trail

All attribute changes include the source in the event log:

```text
User user@myaccessid.org has been updated. Source: isd:puhuri. Details:
first_name: OldName -> NewName
email: old@example.com -> new@example.com
```

The `_change_source` annotation is set on the user instance before saving, and picked up by the `log_user_save` signal handler. Full provenance history is preserved in the Event Log even though `attribute_sources` only tracks the last writer.

## Security

### Permission Model

```mermaid
flowchart TD
    Caller["API Caller"]
    Check1{"Is staff?"}
    Check2{"is_identity_manager?"}
    Check3{"source in managed_isds?"}
    Allow["Allow"]
    Deny["403 Forbidden"]

    Caller --> Check1
    Check1 -->|Yes| Allow
    Check1 -->|No| Check2
    Check2 -->|No| Deny
    Check2 -->|Yes| Check3
    Check3 -->|Yes| Allow
    Check3 -->|No| Deny
```

### Threat Mitigations

| Threat | Mitigation |
|--------|-----------|
| Source spoofing | `managed_isds` scopes each identity manager to specific ISDs |
| Mass user creation | `FEDERATED_IDENTITY_SYNC_ENABLED` feature flag (default off) |
| Concurrent writes | `select_for_update()` serializes writes per user |
| Attribute injection | Three-way field intersection restricts which fields are accepted |

## Examples

### Example 1: Setting Up a Puhuri Identity Manager

```bash
# 1. Create a staff-managed user account for the Puhuri ISD operator
curl -X PATCH https://api.waldur.example.com/api/users/<uuid>/ \
  -H "Authorization: Token STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "is_identity_manager": true,
    "managed_isds": ["isd:puhuri"]
  }'

# 2. Enable the Identity Bridge
curl -X PATCH https://api.waldur.example.com/api/configuration/ \
  -H "Authorization: Token STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "FEDERATED_IDENTITY_SYNC_ENABLED": true
  }'
```

### Example 2: Pushing User Attributes from Puhuri

```bash
curl -X POST https://api.waldur.example.com/api/identity-bridge/ \
  -H "Authorization: Token PUHURI_MANAGER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice@myaccessid.org",
    "source": "isd:puhuri",
    "first_name": "Alice",
    "last_name": "Smith",
    "email": "alice@cern.ch",
    "organization": "CERN",
    "affiliations": ["member@cern.ch"]
  }'
```

### Example 3: Removing a User from Puhuri

```bash
curl -X POST https://api.waldur.example.com/api/identity-bridge/remove/ \
  -H "Authorization: Token PUHURI_MANAGER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice@myaccessid.org",
    "source": "isd:puhuri"
  }'
```

### Example 4: Multi-ISD Scenario

```mermaid
sequenceDiagram
    participant EOSC as EOSC
    participant PUHURI as Puhuri
    participant W as Waldur
    participant U as User_alice

    EOSC->>W: Push {email: "alice@uni.eu", org: "University"}
    Note over U: email owned by isd:eosc<br/>org owned by isd:eosc<br/>active_isds: [isd:eosc]

    PUHURI->>W: Push {email: "alice@cern.ch", org: ""}
    Note over U: email ownership → isd:puhuri<br/>org preserved (empty from non-owner)<br/>active_isds: [isd:eosc, isd:puhuri]

    EOSC->>W: Remove alice from isd:eosc
    Note over U: Clear EOSC-owned fields<br/>active_isds: [isd:puhuri]<br/>User stays active

    PUHURI->>W: Remove alice from isd:puhuri
    Note over U: active_isds: []<br/>User DEACTIVATED
```

## Diagnostics and Reporting

### User Serializer Fields

Staff users see the following Identity Bridge fields on `GET /api/users/{uuid}/`:

| Field | Type | Description |
|-------|------|-------------|
| `attribute_sources` | object | Per-attribute source and timestamp tracking |
| `active_isds` | list | ISDs that have asserted this user exists |
| `managed_isds` | list | ISDs this user can manage (if identity manager) |
| `is_identity_manager` | boolean | Whether user is an identity manager |

These fields are **hidden from non-staff users** and are always read-only.

### Per-User Status

**Endpoint**: `GET /api/users/{uuid}/identity_bridge_status/`

Staff-only. Returns diagnostic information about a specific user's identity bridge state, including staleness detection.

```json
{
  "active_isds": ["isd:eosc", "isd:puhuri", "isd:efp"],
  "managed_isds": [],
  "attribute_sources": {
    "email": {
      "source": "isd:eosc",
      "timestamp": "2026-02-05T12:00:00Z",
      "age_days": 0.5,
      "is_stale": false
    },
    "organization": {
      "source": "isd:puhuri",
      "timestamp": "2026-01-20T09:30:00Z",
      "age_days": 16.1,
      "is_stale": true
    }
  },
  "stale_attributes": ["organization"],
  "effective_bridge_fields": ["affiliations", "email", "first_name", "last_name", "organization"],
  "is_federated": true
}
```

| Field | Description |
|-------|-------------|
| `attribute_sources` | Each attribute enriched with `age_days` and `is_stale` (threshold: 7 days) |
| `stale_attributes` | Fields not refreshed within the threshold — detects sync failures |
| `effective_bridge_fields` | Current three-way intersection result — what CAN be synced right now |
| `is_federated` | Whether this user has any active ISD connections |

### System-Wide Statistics

**Endpoint**: `GET /api/identity-bridge/stats/`

Staff-only. Returns aggregate statistics across all federated users. Does **not** require `FEDERATED_IDENTITY_SYNC_ENABLED` to be on (reports disabled state).

```json
{
  "enabled": true,
  "deactivation_policy": "all_isds_removed",
  "allowed_attributes": ["affiliations", "email", "first_name", "last_name", "organization"],
  "total_federated_users": 1250,
  "total_active_federated_users": 1180,
  "users_per_isd": [
    {
      "isd": "isd:eosc",
      "user_count": 800,
      "stale_user_count": 12,
      "oldest_sync": "2026-01-15T08:00:00Z"
    },
    {
      "isd": "isd:puhuri",
      "user_count": 650,
      "stale_user_count": 3,
      "oldest_sync": "2026-01-28T14:00:00Z"
    },
    {
      "isd": "isd:efp",
      "user_count": 420,
      "stale_user_count": 0,
      "oldest_sync": "2026-02-04T16:00:00Z"
    }
  ],
  "stale_threshold_days": 7
}
```

| Field | Description |
|-------|-------------|
| `users_per_isd` | Per-ISD breakdown sorted by user count (descending) |
| `stale_user_count` | Users whose most recent attribute from this ISD is older than the threshold |
| `oldest_sync` | Oldest attribute timestamp from this ISD — detects if an ISD stopped syncing |
| `total_federated_users` | Users with non-empty `active_isds` (includes deactivated) |
| `total_active_federated_users` | Active users with non-empty `active_isds` |

### Operational Use Cases

```mermaid
flowchart LR
    subgraph Detect["What to detect"]
        D1["ISD stopped syncing"]
        D2["Stale user attributes"]
        D3["ISD user count drop"]
        D4["Misconfigured bridge fields"]
    end

    subgraph Endpoint["Which endpoint"]
        E1["GET /api/identity-bridge/stats/"]
        E2["GET /api/users/&#123;uuid&#125;/identity_bridge_status/"]
    end

    D1 --> E1
    D2 --> E2
    D3 --> E1
    D4 --> E1
```

| Scenario | What to check | Endpoint |
|----------|--------------|----------|
| "Is the EOSC sync working?" | `users_per_isd` entry for `isd:eosc` — check `stale_user_count` is low | `/api/identity-bridge/stats/` |
| "Why does this user have old data?" | `attribute_sources[field].age_days` per field | `/api/users/{uuid}/identity_bridge_status/` |
| "Which fields can ISDs actually write?" | `effective_bridge_fields` or `allowed_attributes` | Either endpoint |
| "Did an ISD lose all its users?" | `users_per_isd` — compare counts over time | `/api/identity-bridge/stats/` |
| "Is this user federated at all?" | `is_federated` + `active_isds` | `/api/users/{uuid}/identity_bridge_status/` |

## Agent Identity and Event Queue Access

ISD identity managers can also register agent identities and create STOMP event subscription queues for offerings, enabling federated site agents to operate without requiring pre-existing offering users.

### Agent Identity Management

ISD identity managers with non-empty `managed_isds` can create agent identities for offerings in Active, Paused, or Unavailable states. Each agent identity tracks its creator via the `created_by` field, and ISD managers can only update, delete, or list their own agent identities.

This solves the bootstrapping problem: agents create offering users, so requiring offering users to register agents would be circular.

See [STOMP-Based Event Notification System — Agent Identity Permissions](core-concepts/event-based-order-processing.md#agent-identity-permissions) for the full permission model.

### Event Subscription Queues

ISD identity managers can create STOMP event subscription queues for offerings in the same allowed states (Active, Paused, Unavailable). Draft and Archived offerings are rejected.

See [Event Subscription Queues — Access Control](guides/event-subscription-queues.md#create-queue) for details.

## See Also

- [User Profile Attributes](user-profile-attributes.md) — attribute reference and configuration
- [Multi-Client OIDC](multi-client-oidc.md) — OIDC authentication and claim mapping
- [Offering Users](core-concepts/offering-users.md) — exposing user attributes to services
- [STOMP Event Notification System](core-concepts/event-based-order-processing.md) — agent identity and event system
- [Event Subscription Queues](guides/event-subscription-queues.md) — queue lifecycle and API
