# User Data Access Tracking

Waldur provides GDPR-compliant transparency features that allow users to see who has access
to their personal data and maintain an audit trail of actual access events.

## Overview

The User Data Access Tracking system consists of two main components:

1. **Data Access Visibility** - Shows who CAN access a user's profile data
2. **Data Access History** - Shows who DID access a user's profile data (audit log)

## Feature Flag

Enable the Data Access tab in user profiles:

```http
PATCH /api/feature-values/
Content-Type: application/json

{
  "user.show_data_access": true
}
```

## Configuration

### Constance Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `USER_DATA_ACCESS_LOGGING_ENABLED` | `False` | Enable logging of user data access events |
| `USER_DATA_ACCESS_LOG_SELF_ACCESS` | `False` | Log when users access their own profile |
| `USER_DATA_ACCESS_LOG_RETENTION_DAYS` | `90` | Days to retain logs before automatic cleanup |

### Logged Fields

Only personal data fields are logged for GDPR compliance. Technical fields (url, uuid, token, permissions, etc.) are excluded.

**Personal data fields tracked**:

- Identity: `username`, `full_name`, `native_name`, `first_name`, `last_name`
- Contact: `email`, `phone_number`
- Professional: `job_title`, `organization`, `organization_country`, `organization_type`, `affiliations`
- Personal: `civil_number`, `birth_date`, `gender`, `personal_title`, `place_of_birth`, `country_of_residence`, `nationality`, `nationalities`
- Other: `eduperson_assurance`

## Access Categories

### Administrative Access

Platform staff and support users have global access to all user data for administrative
purposes. This access is inherent to their roles.

| Accessor Type | Description |
|---------------|-------------|
| `staff` | Platform administrators |
| `support` | Platform support staff |
| `staff_and_support` | Users with both roles |

### Organizational Access

Users within the same organization (customer) or project can see basic profile information
of their peers. This is based on role assignments.

### Service Provider Access

When users consent to share data with service providers via marketplace offerings,
providers can access specific profile fields configured in the offering's
`OfferingUserAttributeConfig`.

## API Endpoints

### User-Specific Endpoints

#### Data Access Visibility

```http
GET /api/users/{uuid}/data_access/
```

Returns who has access to a specific user's profile data.

**Permissions**: Own profile, or staff/support for any user.

**Response** (regular user view):

```json
{
  "administrative_access": {
    "description": "Platform administrators with global access to all user data"
  },
  "organizational_access": [
    {
      "scope_type": "customer",
      "scope_uuid": "...",
      "scope_name": "Example Organization",
      "users": [
        {
          "user_uuid": "...",
          "username": "colleague",
          "full_name": "Colleague Name",
          "role": "owner"
        }
      ]
    }
  ],
  "service_provider_access": [...],
  "summary": {
    "total_administrative_access": null,
    "total_organizational_access": 12,
    "total_provider_access": 3
  }
}
```

**Note**: Regular users do not see admin counts or individual admin users. Staff/support
users see full details including `staff_count`, `support_count`, and `users` list.

#### Data Access History

```http
GET /api/users/{uuid}/data_access_history/
```

Returns historical audit log of who accessed a user's profile data.

**Permissions**: Own profile, or staff/support for any user.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `start_date` | DATE | Filter from this date (inclusive) |
| `end_date` | DATE | Filter until this date (inclusive) |
| `accessor_type` | STRING | Filter by accessor type |

**Response** (regular user view - anonymized):

```json
[
  {
    "uuid": "...",
    "timestamp": "2026-01-22T10:00:00Z",
    "accessor_type": "staff",
    "accessed_fields": ["email", "full_name"],
    "accessor_category": "Platform administrator"
  }
]
```

**Response** (staff/support view - full details):

```json
[
  {
    "uuid": "...",
    "timestamp": "2026-01-22T10:00:00Z",
    "accessor_type": "staff",
    "accessed_fields": ["email", "full_name"],
    "accessor": {
      "uuid": "...",
      "username": "admin_user",
      "full_name": "Admin User"
    },
    "ip_address": "192.168.1.100",
    "context": {
      "endpoint": "/api/users/abc123/",
      "method": "GET"
    }
  }
]
```

### Global Admin Endpoint

#### Global Data Access Logs

```http
GET /api/data-access-logs/
```

Returns all data access logs across the platform.

**Permissions**:

| Action | Staff | Support | Regular User |
|--------|-------|---------|--------------|
| List/View | ✅ | ✅ | ❌ |
| Delete | ✅ | ❌ | ❌ |

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `page` | INTEGER | Page number |
| `page_size` | INTEGER | Results per page |
| `start_date` | DATE | Filter from this date |
| `end_date` | DATE | Filter until this date |
| `accessor_type` | STRING | Filter by accessor type |
| `user_uuid` | UUID | Filter by target user |
| `accessor_uuid` | UUID | Filter by accessor |
| `query` | STRING | Full-text search |
| `o` | STRING | Ordering field |

**Ordering options**: `timestamp`, `-timestamp`, `accessor_type`, `-accessor_type`,
`user_username`, `-user_username`, `accessor_username`, `-accessor_username`

#### Delete a Log Entry

```http
DELETE /api/data-access-logs/{uuid}/
```

Deletes a specific data access log entry.

**Permissions**: Staff only (support users cannot delete).

**Response**: `204 No Content` on success.

**Response**:

```json
{
  "count": 1250,
  "next": "...",
  "previous": null,
  "results": [
    {
      "uuid": "...",
      "timestamp": "2026-01-22T10:00:00Z",
      "accessor_type": "staff",
      "accessed_fields": ["email", "full_name"],
      "user": {
        "uuid": "...",
        "username": "target_user",
        "full_name": "Target User"
      },
      "accessor": {
        "uuid": "...",
        "username": "admin_user",
        "full_name": "Admin User"
      },
      "ip_address": "192.168.1.100",
      "context": {...}
    }
  ]
}
```

## Accessor Types

| Type | Description | Anonymized Label |
|------|-------------|------------------|
| `staff` | Platform administrator | "Platform administrator" |
| `support` | Support staff | "Platform support staff" |
| `organization_member` | User in same org/project | "User in your organization" |
| `service_provider` | Provider via consent | "Service provider" |
| `self` | User accessing own data | "You" |

## Tiered Visibility Model

The API implements privacy-preserving tiered visibility:

**Regular users see**:

- Administrative access: Description only (no counts, no names)
- Organizational access: Full peer details (names, roles)
- Service provider access: Offerings and exposed fields
- History: Anonymized accessor categories

**Staff/Support users see**:

- Administrative access: Counts and individual admin users
- Organizational access: Full details
- Service provider access: Including provider team members
- History: Full accessor identity, IP address, and context

## Data Model

### UserDataAccessLog

Stores audit trail of user data access events.

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | UUID | Unique identifier |
| `timestamp` | DateTime | When access occurred |
| `target_user` | FK(User) | User whose data was accessed |
| `accessor` | FK(User) | User who accessed the data |
| `accessor_type` | String | Type of accessor |
| `accessed_fields` | JSONField | List of fields accessed |
| `ip_address` | IPAddress | IP address of accessor |
| `context` | JSONField | Additional context (endpoint, method) |

## Related Documentation

- [User Profile Attributes](./user-profile-attributes.md) - Profile attribute reference
- [Per-Offering User Attribute Configuration](./core-concepts/offering-users.md) - Provider attribute exposure
