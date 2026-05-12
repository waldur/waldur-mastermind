# Offering Groups

Offering groups let a service provider express that several offerings belong to the same logical entity (for example, the partitions of a SLURM cluster exposed as separate offerings). Groups are a generic grouping primitive — title, description and icon — not tied to any specific backend type.

## Overview

A common pattern is one cluster split into multiple offerings (one per partition or per node type). Without grouping, those offerings appear flat in the catalog and there is no canonical way to say "these belong together". `OfferingGroup` solves this by:

- giving providers a single object to attach metadata to (title, description, icon);
- letting consumers list all offerings in the same logical entity with one filter;
- keeping the grouping per-customer, so providers manage only their own groups.

## Constraints

1. **Same customer**: every offering in a group must belong to the same `customer` as the group. The API rejects assignments that span customers.
2. **Provider-scoped management**: service providers manage their own groups; staff have full access. Other authenticated users do not see them.
3. **Generic grouping**: groups carry presentation metadata only — they do not change billing, ordering or any provisioning behaviour. Whether an offering is part of a group is purely declarative.
4. **SET_NULL on delete**: deleting a group does not cascade. Offerings that pointed at it have their `offering_group` cleared.

## Permission Model

| Action | Staff | Service Provider (own customer) | Service Provider (other) | Regular User |
|--------|-------|----------------------------------|---------------------------|--------------|
| List/Retrieve groups | All | Own customer's groups | None | None |
| Create group | Yes | Yes (customer must be a service provider) | No | No |
| Update / Delete group | Yes | Yes | No | No |
| Assign offering to group (`set_offering_group`) | Yes (any state) | Yes (DRAFT offerings only) | No | No |

The list endpoint uses the project's `GenericRoleFilter` against the group's `customer`, so an organization owner only sees groups belonging to organizations they have a role on.

`customer` is part of `protected_fields` and cannot be reassigned after creation — to move a group between customers, delete and recreate it.

The `set_offering_group` action reuses `can_update_offering`, which mirrors the regular offering `update` permission: service-provider owners can mutate the assignment only on offerings in the `DRAFT` state; staff can mutate any state. The server enforces this regardless of what the client shows.

## OfferingGroup Model

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | UUID | Unique identifier |
| `title` | String (255 chars) | Display title |
| `description` | Text | Optional description (inherits from `DescribableMixin`, ≤4096 chars) |
| `icon` | File | Optional icon image |
| `customer` | FK → Customer | The owning service-provider customer |
| `created` | DateTime | Creation timestamp |

The reverse relation is `Customer.offering_groups` (all groups owned by a customer) and `OfferingGroup.offerings` (all offerings currently in the group).

## API Endpoints

### Group Management

```bash
# List groups visible to the caller
GET /api/marketplace-offering-groups/
# Filters: ?title=<icontains>  ?customer_uuid=<uuid>

# Create a group
POST /api/marketplace-offering-groups/
{
  "title": "cluster-alpha",
  "description": "All partitions of the alpha cluster",
  "customer": "http://.../api/customers/<customer-uuid>/"
}

# Retrieve a group
GET /api/marketplace-offering-groups/<uuid>/

# Update title / description / icon (customer is protected)
PATCH /api/marketplace-offering-groups/<uuid>/
{ "description": "Updated description" }

# Delete a group (offerings get offering_group=NULL)
DELETE /api/marketplace-offering-groups/<uuid>/
```

### Assign or Clear an Offering's Group

Two ways to set the group on an offering:

**At creation**, include `offering_group` in the create payload. The group's customer must match the offering's customer:

```bash
POST /api/marketplace-provider-offerings/
{
  "name": "alpha-cpu",
  "customer": "http://.../api/customers/<customer-uuid>/",
  "category": "http://.../api/marketplace-categories/<uuid>/",
  "type": "Support.OfferingTemplate",
  "offering_group": "<group-uuid>"
}
```

**After creation**, use the dedicated action (mirrors `set_profile`):

```bash
# Assign
POST /api/marketplace-provider-offerings/<offering-uuid>/set_offering_group/
{ "offering_group": "<group-uuid>" }

# Clear
POST /api/marketplace-provider-offerings/<offering-uuid>/set_offering_group/
{ "offering_group": null }
```

Response echoes the resulting state:

```json
{
  "offering_group_uuid": "...",
  "offering_group_title": "cluster-alpha"
}
```

### Listing Offerings in a Group

There is no nested route — filter the existing provider-offering list by group UUID. This keeps pagination, permissions and existing filters intact:

```bash
GET /api/marketplace-provider-offerings/?offering_group_uuid=<group-uuid>
```

### Reading Group Fields on Offerings

Both the slim and full offering serializers expose three read-only group fields:

| Field | Where | Description |
|-------|-------|-------------|
| `offering_group` | `ProviderOfferingDetailsSerializer` (full) | Hyperlink to the group, or `null` |
| `offering_group_uuid` | both | UUID of the assigned group, or `null` |
| `offering_group_title` | both | Title of the assigned group, or `null` |

The slim serializer (`ProviderOfferingSerializer`, used by `/api/marketplace-service-providers/<uuid>/offerings/`) exposes the `_uuid` and `_title` pair so a UI table can show the group column and a row action can pre-populate the current group without an extra fetch.

## Response Format

### Group Response

```json
{
  "url": "http://example.com/api/marketplace-offering-groups/abc.../",
  "uuid": "abc...",
  "created": "2026-05-12T10:30:00Z",
  "title": "cluster-alpha",
  "description": "All partitions of the alpha cluster",
  "icon": null,
  "customer": "http://example.com/api/customers/cus.../",
  "customer_uuid": "cus...",
  "customer_name": "Acme HPC"
}
```

### Offering Response (excerpt)

```json
{
  "uuid": "off...",
  "name": "alpha-cpu",
  "offering_group": "http://example.com/api/marketplace-offering-groups/abc.../",
  "offering_group_uuid": "abc...",
  "offering_group_title": "cluster-alpha"
}
```

## Filter Reference

### Group List

| Parameter | Description |
|-----------|-------------|
| `title` | Group title (case-insensitive contains) |
| `customer_uuid` | UUID of the owning customer |

### Offering List

| Parameter | Description |
|-----------|-------------|
| `offering_group_uuid` | Show only offerings assigned to this group |

## Use Cases

### 1. SLURM cluster partitions

A SLURM cluster exposing each partition as a separate offering:

```bash
# Create the cluster group
POST /api/marketplace-offering-groups/
{ "title": "cluster-alpha", "customer": "<customer-url>" }
# → uuid: G

# Assign each partition offering to it
POST /api/marketplace-provider-offerings/<cpu-uuid>/set_offering_group/   { "offering_group": "<G>" }
POST /api/marketplace-provider-offerings/<gpu-uuid>/set_offering_group/   { "offering_group": "<G>" }
POST /api/marketplace-provider-offerings/<bulk-uuid>/set_offering_group/  { "offering_group": "<G>" }

# Show "all partitions of cluster-alpha"
GET  /api/marketplace-provider-offerings/?offering_group_uuid=<G>
```

### 2. Storage tiers under one storage backend

```bash
POST /api/marketplace-offering-groups/
{ "title": "ceph-prod", "description": "Production Ceph cluster (replicated and erasure-coded pools)" }
# Assign hot / warm / cold storage offerings to the same group
```

### 3. VM families

```bash
POST /api/marketplace-offering-groups/
{ "title": "general-purpose", "description": "Balanced VM family" }
```

## Best Practices

1. **One group per logical entity** — a SLURM cluster, a Ceph cluster, a VM family. Don't use groups as a freeform tag system; that's what offering tags are for.
2. **Stable titles** — the title is what consumers see. Avoid renames after offerings are in use.
3. **Mind the DRAFT-only constraint** — service-provider owners can only reassign groups while the offering is still in `DRAFT`. Plan grouping early, or have a staff user do it.
4. **Delete is safe** — `SET_NULL` means deleting a group will not remove the offerings; it only detaches them. Useful for reorganizations.
5. **Pair with categories, not against them** — categories classify *what* the offering is; groups express *which logical entity* the offering belongs to. Use both.
