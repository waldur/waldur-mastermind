# Course Accounts in Waldur

## Overview

Course accounts provide temporary access management for educational and training purposes within Waldur. This feature is **optional** and enables integration with external learning management systems to provide time-limited access to resources for course participants.

## Architecture

### Core Components

#### Course Account Model

Located in `src/waldur_mastermind/marketplace/models.py`, the CourseAccount model provides the foundation for course-based access management:

```python
class CourseAccount(
    core_models.UuidMixin,
    TimeStampedModel,
    LoggableMixin,
    core_models.ErrorMessageMixin,
    core_models.DescribableMixin,
):
    project = models.ForeignKey(
        to=structure_models.Project,
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        to=core_models.User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    state = FSMIntegerField(
        default=CourseAccountState.OK,
        choices=CourseAccountState.choices,
    )
    email = models.EmailField(max_length=320, default="")
```

### Key Features

- **Project Scoped**: Course accounts are always associated with projects of kind `COURSE`
- **User Linkage**: Each course account creates and links to a Waldur user
- **State Management**: Accounts have state tracking (OK, CLOSED, ERRED)
- **Email Tracking**: Stores email for participant communication
- **Error Handling**: Built-in error message and traceback storage

### State Management

Course accounts use a finite state machine with three states:

1. **OK** (0): Account is active and operational
2. **CLOSED** (1): Account has been closed/deactivated
3. **ERRED** (2): Account is in error state

State transitions:

- `set_state_ok()`: ERRED → OK
- `set_state_closed()`: OK/ERRED → CLOSED
- `set_state_erred()`: * → ERRED

## Backend Integration

### Configuration Settings

Course account functionality requires the following settings in `WALDUR_CORE`:

```python
# Enable/disable course account API integration
COURSE_ACCOUNT_USE_API = False  # Default: disabled

# External course account management endpoints
COURSE_ACCOUNT_URL = ""  # Webhook URL for course account management
COURSE_ACCOUNT_TOKEN_URL = ""  # OAuth2 token endpoint
COURSE_ACCOUNT_TOKEN_CLIENT_ID = ""  # OAuth2 client ID
COURSE_ACCOUNT_TOKEN_SECRET = ""  # OAuth2 client secret
```

### Mock Mode

For development and testing, a mock backend can be enabled:

```python
ENABLE_MOCK_COURSE_ACCOUNT_BACKEND = True
```

This simulates course account operations without requiring external backend connections.

## Backend API Requirements

When `COURSE_ACCOUNT_USE_API` is enabled, the backend must implement the following endpoints:

### 1. Authentication Endpoint

**POST** `{COURSE_ACCOUNT_TOKEN_URL}`

Request:

```text
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id={COURSE_ACCOUNT_TOKEN_CLIENT_ID}
&client_secret={COURSE_ACCOUNT_TOKEN_SECRET}
```

Response:

```json
{
    "access_token": "bearer-token-string"
}
```

### 2. Create Course Account

**POST** `{COURSE_ACCOUNT_URL}`

Headers:

```text
Authorization: Bearer {access_token}
Content-Type: application/json
```

Request Body:

```json
{
    "email": "participant@example.com",
    "description": "Course participant account",
    "project": {
        "uuid": "project-uuid",
        "name": "Course Project Name"
    },
    "owner": {
        "username": "instructor-username",
        "email": "instructor@example.com"
    }
}
```

Response:

```json
{
    "tempAccount": {
        "username": "generated-username",
        "email": "participant@example.com",
        "status": "active",
        "createdAt": "2025-01-01T12:00:00Z",
        "expiresAt": "2025-02-01T12:00:00Z"
    }
}
```

### 3. Get Course Account

**GET** `{COURSE_ACCOUNT_URL}/{username}`

Headers:

```text
Authorization: Bearer {access_token}
```

Response:

```json
{
    "tempAccount": {
        "username": "username",
        "email": "participant@example.com",
        "status": "active",
        "createdAt": "2025-01-01T12:00:00Z",
        "expiresAt": "2025-02-01T12:00:00Z"
    }
}
```

### 4. Close Course Account

**PUT** `{COURSE_ACCOUNT_URL}/{username}/close`

Headers:

```text
Authorization: Bearer {access_token}
```

Response:

```json
{
    "tempAccount": {
        "status": "closed",
        "disabledDate": "2025-01-01T12:00:00Z"
    }
}
```

### 5. Bulk Create Course Accounts

The backend should support batch creation of course accounts through the same POST endpoint, accepting an array of account objects.

## API Endpoints (Frontend)

### Course Account Management

- **List**: `GET /api/marketplace-course-accounts/`
- **Create**: `POST /api/marketplace-course-accounts/`
- **Retrieve**: `GET /api/marketplace-course-accounts/{uuid}/`
- **Delete**: `DELETE /api/marketplace-course-accounts/{uuid}/`
- **Bulk Create**: `POST /api/marketplace-course-accounts/create_bulk/`

Note: Update operations are disabled for course accounts to maintain consistency with external systems.

### API Response Fields

Each course account includes the following fields:

#### Basic Fields

- `uuid`: Unique identifier for the course account
- `created`: Account creation timestamp
- `modified`: Last modification timestamp
- `email`: Participant email address
- `description`: Optional description of the account
- `state`: Current account state (OK, Closed, Erred)

#### User Information

- `user_uuid`: UUID of the linked Waldur user
- `username`: Username of the linked user

#### Project Information

- `project`: UUID of the parent course project
- `project_uuid`: Same as project (for consistency)
- `project_name`: Name of the course project
- `project_slug`: URL-friendly project identifier
- `project_start_date`: Course project start date (if set)
- `project_end_date`: Course project end date (required for course projects)

#### Customer Information

- `customer_uuid`: UUID of the customer that owns the project
- `customer_name`: Name of the customer organization

#### Error Tracking

- `error_message`: Human-readable error description (if account is in error state)
- `error_traceback`: Technical error details for debugging

### Filtering and Ordering

The course accounts list endpoint supports comprehensive filtering and ordering options:

#### Basic Filters

- `username`: Filter by username (exact match)
- `email`: Filter by email (case-insensitive contains)
- `state`: Filter by account state (OK, Closed, Erred)
- `project_uuid`: Filter by project UUID

#### Date Range Filters

- `project_start_date_after`: Show accounts where project starts after this date
- `project_start_date_before`: Show accounts where project starts before this date
- `project_end_date_after`: Show accounts where project ends after this date
- `project_end_date_before`: Show accounts where project ends before this date

#### Ordering Options

Use the `o` parameter to order results by:

- `created`: Creation date
- `modified`: Modification date
- `state`: Account state
- `email`: Email address
- `username`: Username
- `project_name`: Project name
- `project_start_date`: Project start date
- `project_end_date`: Project end date

Add `-` prefix for descending order (e.g., `o=-created`).

### Example API Requests

#### Basic Listing

```bash
GET /api/marketplace-course-accounts/
```

#### Filter by Active Accounts

```bash
GET /api/marketplace-course-accounts/?state=OK
```

#### Filter by Email Contains

```bash
GET /api/marketplace-course-accounts/?email=student
```

#### Filter by Project Date Range

```bash
GET /api/marketplace-course-accounts/?project_start_date_after=2024-01-01&project_end_date_before=2024-12-31
```

#### Combine Filters with Ordering

```bash
GET /api/marketplace-course-accounts/?state=OK&o=-created
```

#### Order by Project Start Date

```bash
GET /api/marketplace-course-accounts/?o=project_start_date
```

## Permissions

Course account operations require the `MANAGE_COURSE_ACCOUNT` permission at the appropriate scope:

- **Create**: Permission required at project or customer level
- **Delete**: Permission required at project or customer level
- **List/View**: Staff, support, or users with MANAGE_COURSE_ACCOUNT permission

## Project Requirements

Course accounts can only be created in projects with `kind=COURSE`. This ensures clear separation between regular projects and educational/training projects.

## Lifecycle Management

### Automatic Cleanup

Course accounts are automatically closed when their parent project is deleted:

- Signal handler: `close_course_accounts_after_project_removal`
- Processes all course accounts in the project
- Attempts to close each account via external API
- Continues even if individual account closures fail

### User Creation

When a course account is created:

1. External API returns account details including username
2. A Waldur user is automatically created with:
  - Username from external system
  - Email from the course account
  - Description: "Course Account"
3. The user is linked to the course account

## Error Handling

Course accounts track errors through:

- **state**: Transitions to ERRED state on failures
- **error_message**: Human-readable error description
- **error_traceback**: Full error traceback for debugging

Failed operations automatically transition accounts to ERRED state, which can be recovered using `set_state_ok()` after resolving issues.

## Event Notifications

Course account operations trigger events that can be subscribed to:

- Account creation
- Account updates
- Account deletion
- State changes

These events can be used for:

- Email notifications to participants
- Integration with learning management systems
- Audit logging

## Integration with Site Agents

Course accounts can be integrated with site agents for automated provisioning:

- Site agents can monitor course account creation
- Automatically provision resources for course participants
- Send welcome emails and access instructions
- Clean up resources when accounts are closed

## Implementation Checklist

When implementing a course account backend, ensure:

- [ ] OAuth2 token endpoint is available and returns bearer tokens
- [ ] Account creation endpoint generates unique usernames
- [ ] Accounts have expiration dates for time-limited access
- [ ] Get operation returns current account status
- [ ] Close operation marks accounts as disabled
- [ ] Bulk creation is supported for efficiency
- [ ] Error responses use standard HTTP status codes
- [ ] All endpoints validate bearer token authentication
- [ ] Account status transitions are logged appropriately

## Security Considerations

1. **Temporary Access**: Course accounts should have time-limited access
2. **Username Generation**: External system must ensure unique usernames
3. **Permission Checks**: All operations validate user permissions at appropriate scope
4. **Audit Logging**: All course account operations are logged for audit trails
5. **Cleanup**: Accounts are automatically closed when parent resources are deleted
6. **Project Isolation**: Course accounts are restricted to COURSE-type projects

## Differences from Service Accounts

| Feature | Course Accounts | Service Accounts |
|---------|----------------|------------------|
| Purpose | Temporary educational access | Long-term automation access |
| Scope | Project-level only | Project and Customer levels |
| API Keys | Not supported | Supported with rotation |
| User Creation | Automatic | Not created |
| Update Operations | Disabled | Enabled |
| Expiration | Expected | Optional |
| Bulk Operations | Supported | Not supported |

## Testing

Mock mode can be enabled for testing without external dependencies:

```python
from waldur_mastermind.marketplace import config
config.ENABLE_MOCK_COURSE_ACCOUNT_BACKEND = True
```

This simulates all backend operations locally, useful for:

- Unit tests
- Development environments
- Demo installations
- Training environments
