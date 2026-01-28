# Resource History API

This guide covers resource-specific details for version history tracking.
For general information about the Version History API, see
[Version History API](version-history-api.md).

## Overview

Marketplace Resources have comprehensive version tracking that captures all
modifications to resource configuration, state, and metadata.

## Endpoints

```http
GET /api/marketplace-resources/{uuid}/history/
GET /api/marketplace-resources/{uuid}/history/at/?timestamp=<ISO 8601>

GET /api/marketplace-provider-resources/{uuid}/history/
GET /api/marketplace-provider-resources/{uuid}/history/at/?timestamp=<ISO 8601>
```

See [Version History API](version-history-api.md) for query parameters and
response format details.

## Tracked Fields

The following resource fields are tracked in version history:

| Field | Description |
|-------|-------------|
| `name` | Resource display name |
| `description` | Resource description |
| `slug` | URL-friendly identifier |
| `state` | Current state (Creating, OK, Erred, etc.) |
| `limits` | Resource quotas and limits |
| `attributes` | Offering-specific attributes |
| `options` | User-configurable options |
| `cost` | Current monthly cost |
| `end_date` | Scheduled termination date |
| `downscaled` | Whether resource is downscaled |
| `restrict_member_access` | Access restriction flag |
| `paused` | Whether resource is paused |
| `plan` | Associated pricing plan |

## Example Response

```json
{
  "id": 42,
  "revision_date": "2024-01-15T14:30:00Z",
  "revision_user": {
    "uuid": "user-uuid-123",
    "username": "admin",
    "full_name": "John Admin"
  },
  "revision_comment": "Slug changed to new-slug",
  "serialized_data": {
    "name": "My Resource",
    "description": "Production database",
    "slug": "new-slug",
    "state": "OK",
    "limits": {"cpu": 4, "ram": 8192},
    "attributes": {},
    "options": {},
    "cost": "150.00",
    "end_date": null,
    "downscaled": false,
    "restrict_member_access": false,
    "paused": false,
    "plan": 123
  }
}
```

## Actions That Create History

The following operations create version history entries:

| Action | Revision Comment |
|--------|------------------|
| Resource update | Updated via REST API |
| `set_slug` | Slug changed to {value} |
| `set_downscaled` | Downscaled changed to {value} |
| `set_paused` | Paused changed to {value} |
| `set_restrict_member_access` | Restrict member access changed to {value} |

## Django Admin Interface

The `ResourceAdmin` class inherits from `VersionAdmin`, providing a "History"
button in the Django admin interface. Staff users can:

- View all versions of a resource
- Compare differences between versions
- See who made each change and when
- Revert to a previous version (if needed)

Access the admin history at:

```text
/admin/marketplace/resource/{id}/history/
```

## Use Cases

### Debugging Configuration Issues

When a resource behaves unexpectedly, check its history to see what changed:

```bash
curl -H "Authorization: Token <token>" \
  "https://waldur.example.com/api/marketplace-resources/abc123/history/"
```

### Investigating Cost Changes

Track when and why resource costs changed by filtering history:

```bash
curl -H "Authorization: Token <token>" \
  "https://waldur.example.com/api/marketplace-resources/abc123/history/?\
created_after=2024-01-01T00:00:00Z"
```

### Point-in-Time Analysis

Check resource state before an incident:

```bash
curl -H "Authorization: Token <token>" \
  "https://waldur.example.com/api/marketplace-resources/abc123/history/at/?\
timestamp=2024-01-15T08:00:00Z"
```

## Related Documentation

- [Version History API](version-history-api.md) - General version history documentation
- [Resource Actions](resource-actions.md) - Custom resource actions
- [Waldur Permissions](waldur-permissions.md) - Permission system details
