# Maintenance Announcement Management

## Overview

The MaintenanceAnnouncement system provides a comprehensive solution for managing and communicating maintenance
activities across Waldur services. Service providers can create, schedule, execute, and track
maintenance announcements through a complete REST API interface. The system automatically integrates
with the AdminAnnouncement system to provide user-facing notifications when maintenance is scheduled.

## Core Concepts

### MaintenanceAnnouncement Model

Maintenance announcements represent planned or emergency maintenance activities that may affect service availability. Each announcement includes:

- **Basic Information**: Name, message, maintenance type
- **External Integration**: Optional reference URL to external maintenance trackers
- **Scheduling**: Planned start/end times, actual start/end times
- **State Management**: FSM-controlled lifecycle states
- **Service Provider**: Associated service provider responsible for the maintenance
- **Impact Tracking**: Affected offerings with specific impact levels
- **AdminAnnouncement Integration**: Automatic creation of user-facing notifications when scheduled

### State Lifecycle

MaintenanceAnnouncement follows a finite state machine (FSM) with the following states:

```text
DRAFT → SCHEDULED ← (unschedule)
  ↓         ↓
CANCELLED   IN_PROGRESS → COMPLETED
```

**State Descriptions:**

- **DRAFT**: Initial state when maintenance is created but not yet published
- **SCHEDULED**: Published maintenance announcement visible to customers
- **IN_PROGRESS**: Maintenance is actively being performed
- **COMPLETED**: Maintenance has finished successfully
- **CANCELLED**: Maintenance was cancelled before completion

## REST API Operations

### Standard CRUD Operations

#### List Maintenance Announcements

```http
GET /api/maintenance-announcements/
```

#### Create Maintenance Announcement

```http
POST /api/maintenance-announcements/
Content-Type: application/json

{
  "name": "Database Server Upgrade",
  "message": "We will be upgrading our database servers to improve performance...",
  "maintenance_type": 4,
  "external_reference_url": "https://maintenance.example.com/ticket/12345",
  "scheduled_start": "2024-01-15T02:00:00Z",
  "scheduled_end": "2024-01-15T04:00:00Z",
  "service_provider": "http://localhost:8000/api/service-providers/{uuid}/"
}
```

**Response:**

```json
{
  "url": "http://localhost:8000/api/maintenance-announcements/{uuid}/",
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "name": "Database Server Upgrade",
  "message": "We will be upgrading our database servers to improve performance...",
  "maintenance_type": 4,
  "external_reference_url": "https://maintenance.example.com/ticket/12345",
  "scheduled_start": "2024-01-15T02:00:00Z",
  "scheduled_end": "2024-01-15T04:00:00Z",
  "service_provider": "http://localhost:8000/api/service-providers/{uuid}/",
  "affected_offerings": [],
  "state": "Draft",
  "created": "2024-01-01T10:00:00Z",
  "modified": "2024-01-01T10:00:00Z"
}
```

#### Retrieve Maintenance Announcement

```http
GET /api/maintenance-announcements/{uuid}/
```

#### Update Maintenance Announcement

```http
PATCH /api/maintenance-announcements/{uuid}/
Content-Type: application/json

{
  "message": "Updated maintenance details...",
  "external_reference_url": "https://servicedesk.company.com/ticket/MAINT-67890"
}
```

#### Delete Maintenance Announcement

```http
DELETE /api/maintenance-announcements/{uuid}/
```

### State Management Actions

All state management actions use POST method and follow the same pattern:

#### Schedule Maintenance

Publishes a draft maintenance announcement, making it visible to customers and automatically creates an associated AdminAnnouncement for user notifications.

```http
POST /api/maintenance-announcements/{uuid}/schedule/
```

**Requirements:**

- Current state must be `DRAFT`
- User must be staff or service provider owner

**Result:**

- State transition: `DRAFT → SCHEDULED`
- **AdminAnnouncement creation**: Automatically creates a user-facing notification
- **Content generation**: Uses maintenance type prefix + message
- **Priority mapping**: Sets appropriate priority based on maintenance type
- **Timing**: Active from `scheduled_start - notify_before_minutes` to `scheduled_end + 1 hour`

#### Unschedule Maintenance

Unpublishes a scheduled maintenance announcement, returning it to draft state and removes the associated AdminAnnouncement.

```http
POST /api/maintenance-announcements/{uuid}/unschedule/
```

**Requirements:**

- Current state must be `SCHEDULED`
- User must be staff or service provider owner

**Result:**

- State transition: `SCHEDULED → DRAFT`
- **AdminAnnouncement cleanup**: Automatically deletes the associated user notification

#### Start Maintenance

Marks maintenance as actively in progress.

```http
POST /api/maintenance-announcements/{uuid}/start_maintenance/
```

**Requirements:**

- Current state must be `SCHEDULED`
- User must be staff or service provider owner
- Automatically sets `actual_start` timestamp

**Result:** `SCHEDULED → IN_PROGRESS`

#### Complete Maintenance

Marks maintenance as successfully completed.

```http
POST /api/maintenance-announcements/{uuid}/complete_maintenance/
```

**Requirements:**

- Current state must be `IN_PROGRESS`
- User must be staff or service provider owner
- Automatically sets `actual_end` timestamp

**Result:** `IN_PROGRESS → COMPLETED`

#### Cancel Maintenance

Cancels maintenance before completion and removes any associated AdminAnnouncement.

```http
POST /api/maintenance-announcements/{uuid}/cancel_maintenance/
```

**Requirements:**

- Current state must be `DRAFT` or `SCHEDULED`
- User must be staff or service provider owner

**Result:**

- State transition: `DRAFT|SCHEDULED → CANCELLED`
- **AdminAnnouncement cleanup**: Automatically deletes the associated user notification (if scheduled)

### Response Format

**Success Response (200 OK):**

```json
{
  "detail": "Maintenance announcement has been [action]"
}
```

**State Validation Error (409 Conflict):**

```json
{
  "detail": "Invalid state transition"
}
```

**Permission Error (404 Not Found):**

```json
{
  "detail": "Not found."
}
```

**Authentication Error (401 Unauthorized):**

```json
{
  "detail": "Authentication credentials were not provided."
}
```

## Maintenance Types

The system supports different types of maintenance activities:

| Value | Type | Description |
|-------|------|-------------|
| 1 | Scheduled | Planned maintenance during scheduled windows |
| 2 | Emergency | Unplanned maintenance for critical issues |
| 3 | Security | Security-related updates and patches |
| 4 | Upgrade | System upgrades and feature deployments |
| 5 | Patch | Minor patches and bug fixes |

## AdminAnnouncement Integration

### Automatic User Notifications

When a MaintenanceAnnouncement transitions from `DRAFT` to `SCHEDULED` state, the system automatically creates an associated AdminAnnouncement to notify users. This integration provides:

#### Content Generation

The AdminAnnouncement content is automatically generated with:

- **Type-specific prefix**: Each maintenance type gets a descriptive emoji prefix
  - 🔧 Scheduled Maintenance
  - 🚨 Emergency Maintenance
  - 🔒 Security Maintenance
  - ⬆️ System Upgrade
  - 🩹 Patch Deployment
- **Original message**: The maintenance message is used as-is
- **Combined format**: `[Type Prefix]: [Original Message]`

**Example:**

```text
MaintenanceAnnouncement message: "Database servers will be upgraded for improved performance"
AdminAnnouncement content: "🔧 Scheduled Maintenance: Database servers will be upgraded for improved performance"
```

#### Priority Mapping

AdminAnnouncement priority is automatically set based on maintenance type:

- **Emergency maintenance** → `DANGER` priority (red alerts)
- **Security maintenance** → `WARNING` priority (yellow alerts)
- **All other types** → `INFORMATION` priority (blue alerts)

#### Timing Configuration

AdminAnnouncements are scheduled using the `MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES` setting:

- **Active from**: `scheduled_start - notify_before_minutes`
- **Active to**: `scheduled_end + 1 hour` (buffer for completion)

Default notify_before_minutes is 60 minutes.

#### Lifecycle Management

The AdminAnnouncement automatically syncs with MaintenanceAnnouncement changes:

- **Content updates**: When maintenance message or type changes
- **Cleanup**: When maintenance is unscheduled or cancelled
- **Deletion handling**: Uses SET_NULL to handle manual AdminAnnouncement deletions

### Enhanced AdminAnnouncement API

When a MaintenanceAnnouncement is scheduled, the associated AdminAnnouncement exposes additional maintenance-related fields through the API:

#### Additional Serializer Fields

```http
GET /api/admin-announcements/{uuid}/
```

**Response includes maintenance fields:**

```json
{
  "uuid": "admin-announcement-uuid",
  "description": "🔧 Scheduled Maintenance: Database servers will be upgraded",
  "type": 1,
  "active_from": "2024-01-15T01:00:00Z",
  "active_to": "2024-01-15T05:00:00Z",
  "maintenance_uuid": "maintenance-announcement-uuid",
  "maintenance_name": "Database Server Upgrade",
  "maintenance_type": "upgrade",
  "maintenance_state": "scheduled",
  "maintenance_scheduled_start": "2024-01-15T02:00:00Z",
  "maintenance_scheduled_end": "2024-01-15T04:00:00Z",
  "maintenance_service_provider": "Example Cloud Services",
  "maintenance_affected_offerings": [
    {
      "uuid": "offering-uuid-1",
      "name": "Database Service",
      "impact_level": "partial_outage",
      "impact_level_display": "Partial outage",
      "impact_description": "Database connections may be intermittent"
    },
    {
      "uuid": "offering-uuid-2",
      "name": "API Gateway",
      "impact_level": "degraded_performance",
      "impact_level_display": "Degraded performance",
      "impact_description": "API responses may be slower than usual"
    }
  ]
}
```

**Field Descriptions:**

- **maintenance_uuid**: UUID of the associated MaintenanceAnnouncement
- **maintenance_name**: Human-readable name of the maintenance
- **maintenance_type**: Type of maintenance (upgrade, emergency, etc.)
- **maintenance_state**: Current FSM state of the maintenance
- **maintenance_scheduled_start/end**: Planned maintenance window
- **maintenance_service_provider**: Name of the responsible service provider
- **maintenance_affected_offerings**: Array of affected offerings with impact details

#### API Integration Notes

- **Conditional fields**: Maintenance fields are only present when AdminAnnouncement has an associated MaintenanceAnnouncement
- **Real-time updates**: Fields automatically update when maintenance details change
- **OpenAPI documentation**: All fields are properly documented with type hints for schema generation
- **Null safety**: Fields return `null` or empty arrays when no maintenance is associated

## Configuration

### Settings

#### MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES

Controls how long before the scheduled maintenance start time that AdminAnnouncements become active.

**Default:** `60` (minutes)

**Usage:**

```python
# settings.py
CONSTANCE_CONFIG = {
    'MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES': (60, 'Minutes before maintenance to show admin announcements'),
}
```

**Effect:**

- AdminAnnouncement `active_from` = `maintenance.scheduled_start - notify_before_minutes`
- Users see the notification from this time until maintenance completion + 1 hour buffer

## External Integration

### External Reference URL

The `external_reference_url` field provides integration with external maintenance tracking systems. This optional field allows you to:

- **Link to external tickets**: Connect maintenance announcements to tickets in systems like ServiceNow, JIRA, or custom trackers
- **Provide additional context**: Reference external documentation, status pages, or monitoring dashboards
- **Enable workflow integration**: Allow teams to track maintenance across multiple systems

**Usage Examples:**

- `https://servicedesk.company.com/ticket/MAINT-12345`
- `https://status.example.com/incidents/2024-01-15-database-upgrade`
- `https://monitoring.company.com/dashboard/maintenance/db-upgrade-2024`

**API Field Details:**

- **Type**: URL field (validates URL format)
- **Required**: No (blank=True)
- **Use Cases**: External ticket references, status page links, monitoring dashboards

## Impact Levels

Each affected offering can specify its expected impact level:

| Value | Level | Description |
|-------|-------|-------------|
| 1 | No Impact | Service remains fully operational |
| 2 | Degraded Performance | Service available but with reduced performance |
| 3 | Partial Outage | Some features unavailable |
| 4 | Full Outage | Service completely unavailable |

## Managing Affected Offerings

Maintenance announcements can specify which offerings will be affected through a separate endpoint. The `affected_offerings` field in the MaintenanceAnnouncement response is read-only and populated from associated MaintenanceAnnouncementOffering records.

### Affected Offerings Workflow

1. **Create the maintenance announcement** (as shown above)
2. **Add affected offerings** using the separate endpoint
3. **The affected_offerings field** will automatically reflect the associations

### Affected Offerings API

#### List Affected Offerings

```http
GET /api/maintenance-announcement-offerings/
```

#### Create Affected Offering

```http
POST /api/maintenance-announcement-offerings/
Content-Type: application/json

{
  "maintenance": "http://localhost:8000/api/maintenance-announcements/{uuid}/",
  "offering": "http://localhost:8000/api/marketplace-offerings/{uuid}/",
  "impact_level": 3,
  "impact_description": "API endpoints will be unavailable during database migration"
}
```

**Response:**

```json
{
  "url": "http://localhost:8000/api/maintenance-announcement-offerings/{uuid}/",
  "uuid": "550e8400-e29b-41d4-a716-446655440001",
  "maintenance": "http://localhost:8000/api/maintenance-announcements/{uuid}/",
  "offering": "http://localhost:8000/api/marketplace-offerings/{uuid}/",
  "impact_level": 3,
  "impact_level_display": "Partial outage",
  "impact_description": "API endpoints will be unavailable during database migration",
  "offering_name": "My Service API"
}
```

#### Retrieve Affected Offering

```http
GET /api/maintenance-announcement-offerings/{uuid}/
```

#### Update Affected Offering

```http
PATCH /api/maintenance-announcement-offerings/{uuid}/
Content-Type: application/json

{
  "impact_level": 2,
  "impact_description": "Minor performance degradation expected"
}
```

#### Delete Affected Offering

```http
DELETE /api/maintenance-announcement-offerings/{uuid}/
```

### Viewing Affected Offerings

Once affected offerings are added, they appear in the MaintenanceAnnouncement response:

```http
GET /api/maintenance-announcements/{uuid}/
```

**Response includes:**

```json
{
  "name": "Database Server Upgrade",
  "affected_offerings": [
    {
      "url": "http://localhost:8000/api/maintenance-announcement-offerings/{uuid}/",
      "uuid": "550e8400-e29b-41d4-a716-446655440001",
      "maintenance": "http://localhost:8000/api/maintenance-announcements/{uuid}/",
      "offering": "http://localhost:8000/api/marketplace-offerings/{uuid}/",
      "impact_level": 3,
      "impact_level_display": "Partial outage",
      "impact_description": "API endpoints will be unavailable during database migration",
      "offering_name": "My Service API"
    }
  ]
}
```

## Permission System

### Authorization Model

MaintenanceAnnouncement uses Waldur's standard permission system:

- **Service Provider Path**: `service_provider__customer`
- **Automatic Filtering**: `GenericRoleFilter` handles visibility
- **Role-Based Access**: Permissions tied to service provider ownership

### User Permissions

**Staff Users:**

- Full access to all maintenance announcements
- Can perform all state transitions
- Can create/update/delete any announcement

**Service Provider Owners:**

- Full access to their service provider's maintenance announcements
- Can perform all state transitions on their announcements
- Can create/update/delete their announcements

**Other Users:**

- No access to maintenance announcements (404 Not Found)
- Cannot view or modify any maintenance data

## Key Points

### Affected Offerings Management

- **Separate Endpoint**: Affected offerings are managed via `/api/maintenance-announcement-offerings/`
- **Read-Only Field**: The `affected_offerings` field in MaintenanceAnnouncement is read-only
- **Two-Step Process**: Create maintenance announcement first, then add affected offerings
- **Automatic Population**: The `affected_offerings` field automatically reflects associated records
- **Individual Management**: Each affected offering can be updated or deleted independently

### Important Notes

- You cannot include `affected_offerings` directly in the MaintenanceAnnouncement POST/PATCH requests
- Affected offerings inherit the same permission model as the parent maintenance announcement
- Impact levels and descriptions are specific to each offering-maintenance relationship
- Deleting a maintenance announcement will cascade delete all associated affected offerings

### AdminAnnouncement Integration Notes

- **Automatic lifecycle**: AdminAnnouncements are automatically created/updated/deleted based on maintenance state
- **Manual deletion handling**: If an AdminAnnouncement is manually deleted, the maintenance relationship is gracefully cleared (SET_NULL)
- **Content synchronization**: AdminAnnouncement content automatically updates when maintenance message or type changes
- **Enhanced API**: AdminAnnouncement API dynamically includes maintenance fields when associated with maintenance
- **No manual management needed**: The integration is fully automated - no manual AdminAnnouncement creation required
