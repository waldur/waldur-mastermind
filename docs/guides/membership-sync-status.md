# Membership Sync Status

## Overview

For offerings whose access control is applied by a site agent on the
provider's backend (for example a Rancher cluster with Keycloak groups), a
role granted in Waldur only takes effect after the agent's next sync cycle
succeeds. Membership sync status makes that propagation visible per member:
the agent reports how each grant landed, team views expose it, and providers
can request an immediate re-sync.

The feature is opt-in per offering via the `enable_membership_sync_status`
plugin option and builds on resource projects (`enable_resource_projects`).
When it is off, team views keep their exact previous shape and no sync data
is collected.

## Model

### ResourceMemberSyncStatus

One row per reported role grant.

- **Fields**: `resource`, `user`, `scope_type`, `resource_project`,
  `role_name`, `state`, `message`, `modified`
- **`scope_type`**: `resource` or `resource_project`
- **`state`**: `synced`, `pending`, `missing_in_idp`, `error`
- Reports are **full-replace** per resource: each report deletes the
  resource's existing rows and stores the submitted set. A grant with no row
  means "the agent has not reported on it" (distinct from any real state).

## API Endpoints

### Reporting statuses (agent → backend)

| Endpoint | Description |
|----------|-------------|
| `POST /api/marketplace-provider-resources/{uuid}/set_membership_sync_statuses/` | Full-replace report of a resource's member sync statuses |

Request body:

```json
{
    "statuses": [
        {
            "username": "alice",
            "scope_type": "resource_project",
            "resource_project_uuid": "<rp-uuid>",
            "role_name": "ingress_manage",
            "state": "missing_in_idp",
            "message": "User not found in Keycloak"
        }
    ]
}
```

- Either `username` or `user_uuid` identifies each member (bulk-resolved).
- Entries whose user (or resource project) cannot be resolved are **skipped**
  and echoed back rather than failing the report:
  `{"stored": <n>, "skipped": [...]}`.
- Returns `409 Conflict` when the offering has not opted in.
- Permission: `RESOURCE.SET_BACKEND_METADATA` at the `offering` or
  `offering.customer` scope (so an offering-scoped agent identity can report).

### Reading statuses (team view join)

| Endpoint | Description |
|----------|-------------|
| `GET /api/marketplace-resources/{uuid}/team_members/` | Team members with per-grant sync fields |

When the offering opts in, every grant in the `roles[]` and
`resource_projects[]` arrays carries `sync_state`, `sync_message` and
`sync_reported_at`. When it does not, those fields are omitted entirely. The
view bulk-loads all grants for the page, so the response cost does not grow
per member.

### Requesting a re-sync (provider/staff → agent)

| Endpoint | Description |
|----------|-------------|
| `POST /api/marketplace-provider-resources/{uuid}/sync_user_roles/` | Ask the offering's agent to re-apply this resource's role grants |

- Permission: staff, or `OFFERING.UPDATE` on the offering.
- Throttled to one request per resource per 30 seconds (`429` otherwise).
- Returns `409 Conflict` when the offering has not opted in.
- Delivery uses the offering's event subscriptions. The published message is
  gated on `SITE_AGENT_OFFERING`, consistent with the rest of the pubsub
  path; agents in polling-only mode apply the change on their next cycle
  regardless, and the indicators refresh once the agent reports back.

## Permissions

A site agent authenticates with an offering-scoped `OFFERING.MANAGER` role.
The endpoints above — and the resource / sub-project state-transition actions
the agent drives (`set_state_ok`, `set_as_erred`, and the
`marketplace-provider-resource-projects` state actions) — accept the
`offering` / `resource.offering` scope alongside the owning customer, so the
agent operates without customer-wide rights. `OFFERING.MANAGER` already
carries the underlying permissions (`RESOURCE.SET_STATE`,
`RESOURCE.SET_BACKEND_METADATA`, `OFFERING.UPDATE`); no `permissions.yaml`
change is required.

## Resource project lifecycle audit

Creation, removal and recovery of resource sub-projects are attributed:

- `ResourceProject.created_by` (SET_NULL) records who created a row, mirroring
  the existing `removed_by`; it is exposed as the nullable
  `created_by_username` serializer field.
- Three event types — `marketplace_resource_project_created`,
  `marketplace_resource_project_removed`,
  `marketplace_resource_project_recovered` — are emitted from the create,
  both delete paths, and recover, scoped to the resource project, resource,
  project and customer (registered in the `PROVIDERS` and `RESOURCES` event
  groups).

## Enabling

```json
{
    "plugin_options": {
        "enable_resource_projects": true,
        "enable_membership_sync_status": true
    }
}
```

The reporting side also requires a site agent version that supports
membership sync reports; older agents keep working and grants simply show no
status.

## Related

- [Resource Projects and Role Management](resource-projects.md) — the model
  and role system this builds on.
- [Agent pub/sub](agent-pubsub.md) — how re-sync requests reach the agent.
- User-facing UI documentation lives in the `waldur-docs` repository under
  `user-guide/service-provider-organization/membership-sync-status.md`.
