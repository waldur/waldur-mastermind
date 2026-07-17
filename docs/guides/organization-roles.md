# Organization-scoped custom roles

## Overview

By default every role is deployment-wide: the system roles (`CUSTOMER.OWNER`,
`PROJECT.MEMBER`, …) are available in every organization. This feature lets
staff give a single organization its own roles:

- **Clone** a system role into an organization. The clone copies the template's
  permissions and is usable **only** within that organization and its projects.
- **Conceal** a system role within an organization, so it can no longer be
  granted there — typically so an organization-specific clone supersedes it.

Standard system roles remain untouched everywhere they are not concealed. The
mechanism reuses the existing `RoleAvailability` allow-list (previously used to
scope roles to offerings) and adds a per-organization deny-list.

## Concepts

### RoleAvailability (allow-list)

A role with **no** `RoleAvailability` records is public — available everywhere
(system-role behaviour). Records restrict a role to those scopes and their
descendants. Organization-scoped clones bind availability to a **Customer**:

| Scenario | `RoleAvailability.scope` | Effect |
|----------|--------------------------|--------|
| System role | (none) | Available in every organization |
| Offering role | Offering X | Only for Offering X's resources |
| Organization role (clone) | Customer C | Only within organization C and its projects |

The customer binding covers the organization's projects for free: granting a
project-scoped clone on a project walks up to the customer via
`get_scope_ancestors`.

### CustomerRoleConcealment (deny-list)

`RoleAvailability` cannot *subtract* a public role from a single organization
(adding an availability record would restrict it everywhere). A
`CustomerRoleConcealment` record does that: while it exists for a `(role,
customer)` pair, the role can no longer be granted within that organization and
is hidden from its role pickers. Existing grants are left untouched.

### Role.template

A clone records the role it was created from in `Role.template` (nullable,
`SET_NULL`), surfaced on the API as `template_uuid` / `template_name`.

## API endpoints

The mutating endpoints below are **staff-only**. The `GET /api/roles/` reads are
available to organization members too (see [Visibility](#visibility)): a member
may list the roles usable within their own organization for pickers, but
`available_for_customer` scoped to an organization the caller does not belong to
returns nothing (so concealment metadata cannot be probed).

| Endpoint | Description |
|----------|-------------|
| `GET /api/roles/?available_for_customer={uuid}` | Roles usable within an organization: system roles + the org's clones, minus concealed ones. Members may query only their own organizations; staff any |
| `POST /api/roles/{uuid}/clone_to_customer/` | Clone this (customer/project) role into an organization |
| `GET /api/customer-role-concealments/` | List concealments (filter: `role_uuid`, `scope_uuid`) |
| `POST /api/customer-role-concealments/` | Conceal a role for an organization |
| `DELETE /api/customer-role-concealments/{uuid}/` | Reveal (remove a concealment) |

### Cloning a role into an organization

```http
POST /api/roles/{template_uuid}/clone_to_customer/
{
    "customer": "9e1132b8...",
    "conceal_template": true
}
```

The clone keeps the template's `content_type` and permission set (including its
translated descriptions), is named `{SCOPE}.{customer_slug}.{suffix}` (e.g.
`PROJECT.acme.MEMBER`), and gets a `RoleAvailability` bound to the customer.
With `conceal_template` (default `true`) the source system role is also concealed
for the organization so the clone supersedes it — avoiding two identically
labelled entries in the pickers.

The organization slug is embedded (rather than an opaque UUID) so the name stays
human-readable. Because a slug can change and is not database-unique, a
`Customer` `post_save` handler re-syncs the clone names when the slug changes,
and a numeric collision suffix (`…-2`) guards global uniqueness if another
organization has taken the same slug.

Only `customer`- and `project`-scoped roles can be cloned. Cloning the same
template into the same organization twice is rejected.

### Concealing / revealing a role

```http
POST /api/customer-role-concealments/
{
    "role": "3f0a…",
    "customer": "9e1132b8…"
}
```

Reveal by deleting the concealment record (`DELETE
/api/customer-role-concealments/{concealment_uuid}/`).

## Enforcement

Availability and concealment are enforced in the low-level `add_user` write
primitive, not only in `validate_role_grant`. This matters because several grant
paths (onboarding, SCIM group sync, autoprovisioning, team-restore) call
`add_user` directly. The rules, both resolved by walking the scope's ancestors:

- **Availability**: if the role has availability records, the scope (or an
  ancestor) must match one, else the grant is refused.
- **Concealment**: if an ancestor organization conceals the role, the grant is
  refused (`"Role is concealed for this organization."`).

Notes:

- **`force=`** — `add_user(..., force=True)` bypasses the policy for the few
  internal grants that must always succeed (e.g. onboarding's initial owner
  grant).
- **Uniform for staff** — the check does not depend on the actor, so it applies
  to staff-initiated grants too; the escape hatch is to reveal the role first.
- **Grandfathering** — concealing (or removing an availability record) blocks new
  grants only; existing `UserRole` rows stay active.
- **Lockout guard** — a concealment is refused if it would remove the last
  grantable role that can add members at that scope (e.g. the last
  owner-capable customer role), so an organization cannot be made ungovernable.

## Visibility

`GET /api/roles/` hides other organizations' private roles from non-staff users:
a customer-scoped role is visible only to members of that organization (directly
or through one of its projects). Public/system and offering-scoped roles stay
visible to everyone, including anonymous requests. Staff and support see all
roles.

## Permissions required

| Action | Requirement |
|--------|-------------|
| Clone a role into an organization | Staff |
| Conceal / reveal a role | Staff |
| List an organization's concealments | Staff |
| Grant a cloned role | The clone must be available for the scope and not concealed; caller needs the scope's create-permission |
