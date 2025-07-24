# OfferingUser States and Management

OfferingUser represents a user account created for a specific marketplace offering. It supports a finite state machine (FSM) that tracks the lifecycle of user account creation, validation, and management.

## States

OfferingUser has the following states:

| State | Description |
|-------|-------------|
| `CREATION_REQUESTED` | Initial state when user account creation is requested |
| `CREATING` | Account is being created by the service provider |
| `PENDING_ACCOUNT_LINKING` | Waiting for user to link their existing account |
| `PENDING_ADDITIONAL_VALIDATION` | Requires additional validation from service provider |
| `OK` | Account is active and ready to use |
| `DELETION_REQUESTED` | Account deletion has been requested |
| `DELETING` | Account is being deleted |
| `DELETED` | Account has been successfully deleted |
| `ERROR` | An error occurred during account management |

## State Transitions

```mermaid
stateDiagram-v2
    [*] --> CREATION_REQUESTED : Account requested

    CREATION_REQUESTED --> CREATING : begin_creating()
    CREATION_REQUESTED --> OK : set_ok()

    CREATING --> PENDING_ACCOUNT_LINKING : set_pending_account_linking()
    CREATING --> PENDING_ADDITIONAL_VALIDATION : set_pending_additional_validation()
    CREATING --> OK : set_ok()

    PENDING_ACCOUNT_LINKING --> OK : set_validation_complete()
    PENDING_ADDITIONAL_VALIDATION --> OK : set_validation_complete()

    OK --> DELETION_REQUESTED : request_deletion()

    DELETION_REQUESTED --> DELETING : set_deleting()
    DELETING --> DELETED : set_deleted()

    CREATION_REQUESTED --> ERROR : set_error()
    CREATING --> ERROR : set_error()
    PENDING_ACCOUNT_LINKING --> ERROR : set_error()
    PENDING_ADDITIONAL_VALIDATION --> ERROR : set_error()
    OK --> ERROR : set_error()
    DELETION_REQUESTED --> ERROR : set_error()
    DELETING --> ERROR : set_error()

    ERROR --> OK : set_ok()
    ERROR --> PENDING_ACCOUNT_LINKING : set_pending_account_linking()
    ERROR --> PENDING_ADDITIONAL_VALIDATION : set_pending_additional_validation()
```

## REST API Endpoints

### State Transition Actions

All state transition endpoints require `UPDATE_OFFERING_USER` permission and are accessed via POST to the offering user detail endpoint with the action suffix.

**Base URL:** `/api/marketplace-offering-users/{uuid}/`

#### Set Pending Additional Validation

```http
POST /api/marketplace-offering-users/{uuid}/set_pending_additional_validation/
Content-Type: application/json

{
  "comment": "Additional documents required for validation"
}
```

**Valid transitions from:** `CREATING`, `ERROR`

#### Set Pending Account Linking

```http
POST /api/marketplace-offering-users/{uuid}/set_pending_account_linking/
Content-Type: application/json

{
  "comment": "Please link your existing service account"
}
```

**Valid transitions from:** `CREATING`, `ERROR`

#### Set Validation Complete

```http
POST /api/marketplace-offering-users/{uuid}/set_validation_complete/
```

**Valid transitions from:** `PENDING_ADDITIONAL_VALIDATION`, `PENDING_ACCOUNT_LINKING`

**Note:** This action clears the `service_provider_comment` field.

### OfferingUser Fields

When retrieving or updating OfferingUser objects, the following state-related fields are available:

- `state` (string, read-only): Current state of the user account
- `service_provider_comment` (string, read-only): Comment from service provider for pending states

## Backward Compatibility

The system maintains backward compatibility with existing integrations:

### Automatic State Transitions

- **Username Assignment**: When a username is assigned to an OfferingUser (via API or `set_offerings_username`), the state automatically transitions to `OK`
- **Creation with Username**: Creating an OfferingUser with a username immediately sets the state to `OK`

### Legacy Endpoints

- `POST /api/marketplace-service-providers/{uuid}/set_offerings_username/` - Bulk username assignment that automatically transitions users to `OK` state

## Usage Examples

### Service Provider Workflow

1. **Initial Creation**: OfferingUser is created with state `CREATION_REQUESTED`
2. **Begin Processing**: Transition to `CREATING` state
3. **Require Validation**: If additional validation needed, transition to `PENDING_ADDITIONAL_VALIDATION` with explanatory comment
4. **Complete Validation**: Once validated, transition to `OK` state
5. **Account Ready**: User can now access the service

## Permissions

State transition endpoints use the `permission_factory` pattern with:

- Permission: `UPDATE_OFFERING_USER`
- Scope: `["offering.customer"]` - User must have permission on the offering's customer

This means users need the `UPDATE_OFFERING_USER` permission on the customer that owns the offering associated with the OfferingUser.

## Filtering OfferingUsers

The OfferingUser list endpoint supports filtering by state to help manage users across different lifecycle stages.

### State Filtering

Filter OfferingUsers by their current state using the `state` query parameter:

```http
GET /api/marketplace-offering-users/?state=Requested
GET /api/marketplace-offering-users/?state=Pending%20additional%20validation
```

#### Available State Filter Values

| Filter Value | State Constant | Description |
|--------------|----------------|-------------|
| `Requested` | `CREATION_REQUESTED` | Users with account creation requested |
| `Creating` | `CREATING` | Users whose accounts are being created |
| `Pending account linking` | `PENDING_ACCOUNT_LINKING` | Users waiting to link existing accounts |
| `Pending additional validation` | `PENDING_ADDITIONAL_VALIDATION` | Users requiring additional validation |
| `OK` | `OK` | Users with active, ready-to-use accounts |
| `Requested deletion` | `DELETION_REQUESTED` | Users with deletion requested |
| `Deleting` | `DELETING` | Users whose accounts are being deleted |
| `Deleted` | `DELETED` | Users with successfully deleted accounts |
| `Error` | `ERROR` | Users with errors during account management |

#### Multiple State Filtering

Filter by multiple states simultaneously:

```http
GET /api/marketplace-offering-users/?state=Requested&state=OK
GET /api/marketplace-offering-users/?state=Pending%20account%20linking&state=Pending%20additional%20validation
```

#### Combining with Other Filters

State filtering can be combined with other available filters:

```http
# Filter by state and offering
GET /api/marketplace-offering-users/?state=OK&offering_uuid=123e4567-e89b-12d3-a456-426614174000

# Filter by state and user
GET /api/marketplace-offering-users/?state=Pending%20additional%20validation&user_uuid=456e7890-e89b-12d3-a456-426614174001

# Filter by state and provider
GET /api/marketplace-offering-users/?state=Creating&provider_uuid=789e0123-e89b-12d3-a456-426614174002
```

#### Error Handling

Invalid state values return HTTP 400 Bad Request:

```http
GET /api/marketplace-offering-users/?state=InvalidState
# Returns: 400 Bad Request with error details
```

### Other Available Filters

The OfferingUser list endpoint also supports these filters:

- `offering_uuid` - Filter by offering UUID
- `user_uuid` - Filter by user UUID
- `user_username` - Filter by user's username (case-insensitive)
- `provider_uuid` - Filter by service provider UUID
- `is_restricted` - Filter by restriction status (boolean)
- `created_before` / `created_after` - Filter by creation date
- `modified_before` / `modified_after` - Filter by modification date
- `query` - General search across offering name, username, and user names

### Practical Filtering Examples

Here are common filtering scenarios for managing OfferingUsers:

#### Find Users Requiring Attention

```http
# Get users needing validation or account linking
GET /api/marketplace-offering-users/?state=Pending%20additional%20validation&state=Pending%20account%20linking

# Get users in error state
GET /api/marketplace-offering-users/?state=Error
```

#### Monitor Service Provider Operations

```http
# Track active creation processes for a specific provider
GET /api/marketplace-offering-users/?provider_uuid=123e4567&state=Creating

# Find successfully created accounts for a provider
GET /api/marketplace-offering-users/?provider_uuid=123e4567&state=OK
```

#### Audit and Reporting

```http
# Get all deleted accounts for audit purposes
GET /api/marketplace-offering-users/?state=Deleted

# Find restricted users across all offerings
GET /api/marketplace-offering-users/?is_restricted=true
```

## Events and Logging

State transitions generate:

- **Event logs**: Recorded in the system event log for audit purposes
- **Application logs**: Logged with user attribution for debugging and monitoring
