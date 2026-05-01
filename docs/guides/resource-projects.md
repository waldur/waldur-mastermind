# Resource Projects and Role Management

## Overview

Resource Projects represent sub-entities within a marketplace Resource. For example, a Rancher cluster (Resource) can contain multiple Rancher projects (ResourceProjects), each with its own user role assignments.

This feature is opt-in per offering via the `enable_resource_projects` plugin option.

## Models

### ResourceProject

A sub-project within a Resource with FSM state management.

- **Fields**: `name`, `description`, `backend_id`, `state`, `limits`, `current_usages`
- **States**: Creating → OK → Updating/Terminating → Terminated, or Erred from any state
- **Constraint**: `unique_together = ("resource", "name")`

### Invitations

Invitations to Resource or ResourceProject scopes are issued through the
unified `waldur_core.users.Invitation` model. The invitation's generic `scope`
field points at a `Resource` or `ResourceProject`, and `role` is a `Role` whose
`content_type` matches the scope. On acceptance the standard `add_user` flow
creates a `UserRole` with that role on that scope.

There is no resource-specific invitation model — the same workflow used for
Customer/Project invitations applies.

## API Endpoints

### Resource Projects

| Endpoint | Description |
|----------|-------------|
| `GET /api/marketplace-resource-projects/` | List resource projects (consumer) |
| `POST /api/marketplace-resource-projects/` | Create resource project |
| `GET /api/marketplace-provider-resource-projects/` | List resource projects (provider) |
| `POST /api/marketplace-provider-resource-projects/{uuid}/set_backend_id/` | Set backend ID |
| `POST /api/marketplace-provider-resource-projects/{uuid}/set_state_ok/` | Mark as provisioned |
| `POST /api/marketplace-provider-resource-projects/{uuid}/set_state_erred/` | Mark as failed |

Both consumer and provider ViewSets include `UserRoleMixin`, providing `list_users`, `add_user`, `update_user`, `delete_user` actions for managing role assignments within resource projects.

### Resource & Resource Project Invitations

Use the unified `/api/user-invitations/` endpoints. Specify `scope` as the
URL of the Resource or ResourceProject and `role` as the UUID of a role
whose `content_type` matches the scope.

| Endpoint | Description |
|----------|-------------|
| `POST /api/user-invitations/` | Create invitation (`scope`, `role`, `email`) |
| `GET /api/user-invitations/?scope_type=resource` | List resource invitations |
| `GET /api/user-invitations/?scope_type=resource_project` | List resource-project invitations |
| `POST /api/user-invitations/{uuid}/cancel/` | Cancel invitation |
| `POST /api/user-invitations/{uuid}/accept/` | Accept invitation |

The `scope_type` filter accepts the keys defined in
`waldur_core.permissions.enums.TYPE_MAP`, including `resource` and
`resource_project`.

### Offering Roles

| Endpoint | Description |
|----------|-------------|
| `GET /api/marketplace-offering-roles/` | List offering-scoped roles |
| `POST /api/marketplace-offering-roles/` | Create a role for an offering |
| `DELETE /api/marketplace-offering-roles/{uuid}/` | Delete a role |

**Filters**: `offering_uuid`, `content_type` (`resource` or `resource_project`), `name`

## Permission System Integration

Resource-level access uses the unified `Role` + `UserRole` permission system from `waldur_core.permissions`.

### How Roles Work

1. **System roles** (Customer, Project, Offering scopes) have names unique per content type
2. **Offering roles** (Resource, ResourceProject scopes) can have duplicate names across offerings — each offering defines its own roles (e.g., Rancher has "Cluster Admin", OpenShift has "Cluster Admin")

### Creating Offering-Specific Roles

Service providers create roles for their offerings via the Offering Roles API:

```http
POST /api/marketplace-offering-roles/
{
    "name": "Cluster Admin",
    "content_type_input": "resource",
    "offering": "<offering-uuid>"
}
```

This creates a `Role(name="Cluster Admin", content_type=Resource)` with a `RoleAvailability` record linking it to the offering.

For ResourceProject-scoped roles:

```http
POST /api/marketplace-offering-roles/
{
    "name": "Project Member",
    "content_type_input": "resource_project",
    "offering": "<offering-uuid>"
}
```

### Assigning Users

Users are assigned to Resources or ResourceProjects via `UserRole`:

```http
POST /api/marketplace-resource-projects/{uuid}/add_user/
{
    "user": "<user-uuid>",
    "role": "<role-uuid>"
}
```

The role's `content_type` must match the scope (Resource or ResourceProject). If the role has `RoleAvailability` restrictions, the scope's offering must match.

### Invitation Flow

For users not yet in the system or requiring explicit acceptance, use the
unified Invitation flow:

1. Manager creates invitation: `POST /api/user-invitations/` with
   `scope=<resource-or-resource-project-url>`, `role=<role-uuid>`, `email=...`
2. The user receives the standard Waldur invitation email.
3. The user logs in and accepts via `POST /api/user-invitations/{uuid}/accept/`.
4. `UserRole` is created, linking the user to the resource (or resource
   project) with the specified role.

Permission to create the invitation is gated by `RESOURCE.CREATE_PERMISSION`
or `RESOURCE_PROJECT.CREATE_PERMISSION` granted at the resource scope, parent
project, or customer.

Email-match enforcement on accept is controlled by the
`ENABLE_STRICT_CHECK_ACCEPTING_INVITATION` Constance setting and is shared
with the rest of the invitation system.

### RoleAvailability

Controls where roles can be assigned. When a role has `RoleAvailability` records, it can only be assigned to scopes within those contexts.

| Scenario | RoleAvailability.scope | Effect |
|----------|----------------------|--------|
| System role | (none) | Available everywhere |
| Offering role | Offering X | Only for Offering X's resources |
| Multi-offering role | Offering X, Offering Y | Available in both offerings |

## Enabling Resource Projects

Add `enable_resource_projects: true` to the offering's `plugin_options`:

```json
{
    "plugin_options": {
        "enable_resource_projects": true
    }
}
```

### Limit-cap policy

`resource_projects_limit_policy` controls how `ResourceProject.limits` are
checked against the parent `Resource.limits`. Per-component
`OfferingComponent.min_value` / `max_value` bounds always apply regardless of
this setting.

| Value | Per-project rule | Aggregate rule |
|---|---|---|
| `none` (default) | — | — |
| `per_project` | `project.limits[c] <= resource.limits[c]` | — |
| `aggregate` | (same as `per_project`) | `sum(p.limits[c] for p in resource.projects) <= resource.limits[c]` |

```json
{
    "plugin_options": {
        "enable_resource_projects": true,
        "resource_projects_limit_policy": "aggregate"
    }
}
```

If `Resource.limits` does not contain an entry for a given component, that
component is unconstrained for projects (the parent never set a cap).

## Permissions Required

| Action | Permission | Granted to |
|--------|-----------|------------|
| Create/manage resource projects | `UPDATE_RESOURCE` | Project members, customer owners |
| Invite users to resources / resource projects | `RESOURCE.CREATE_PERMISSION`, `RESOURCE_PROJECT.CREATE_PERMISSION` | Customer owners, project admins, project managers |
| Manage offering roles | `UPDATE_OFFERING` | Offering customer owners |
| Manage user roles on resources | `RESOURCE.CREATE/UPDATE/DELETE_PERMISSION` | Customer owners, project admins, project managers |
| Manage user roles on resource projects | `RESOURCE_PROJECT.CREATE/UPDATE/DELETE_PERMISSION` | Customer owners, project admins, project managers |
| Provider state management | `UPDATE_OFFERING` | Service provider staff |

Permissions are defined in `docker/rootfs/etc/waldur/permissions.yaml`.
