# Service Accounts in Waldur

## Overview

Service accounts provide automated, programmatic access to Waldur resources at various organizational levels. This feature is **optional** and can be enabled to support integration with external authentication systems and automation workflows.

## Architecture

### Core Components

#### 1. ServiceAccountMixin

Located in `src/waldur_core/structure/models.py`, this mixin provides the foundational capability for service account management:

```python
class ServiceAccountMixin(models.Model):
    class Meta:
        abstract = True

    max_service_accounts = models.PositiveSmallIntegerField(
        default=0,
        help_text=_("Maximum number of service accounts allowed"),
        null=True,
        blank=True,
    )
```

This mixin is applied to both `Customer` and `Project` models, enabling service account limits at organizational and project levels.

#### 2. Service Account Hierarchy

```text
BaseServiceAccount (Abstract)
├── ScopedServiceAccount (Abstract)
│   ├── ProjectServiceAccount
│   └── CustomerServiceAccount
└── RobotAccount
```

- **BaseServiceAccount**: Abstract base providing common fields (username, description, state)
- **ScopedServiceAccount**: Extends BaseServiceAccount with email and preferred_identifier
- **ProjectServiceAccount**: Service accounts scoped to specific projects
- **CustomerServiceAccount**: Service accounts scoped to customer organizations
- **RobotAccount**: Automated accounts for resource-level access

### State Management

Service accounts use a finite state machine with three states:

1. **OK** (0): Account is active and operational
2. **CLOSED** (1): Account has been closed/deactivated
3. **ERRED** (2): Account is in error state

State transitions:

- `set_state_ok()`: ERRED → OK
- `set_state_closed()`: OK/ERRED → CLOSED
- `set_state_erred()`: * → ERRED

## Backend Integration

### Configuration Settings

Service account functionality requires the following settings in `WALDUR_CORE`:

```python
# Enable/disable service account API integration
SERVICE_ACCOUNT_USE_API = False  # Default: disabled

# External service account management endpoints
SERVICE_ACCOUNT_URL = ""  # Webhook URL for service account management
SERVICE_ACCOUNT_TOKEN_URL = ""  # OAuth2 token endpoint
SERVICE_ACCOUNT_TOKEN_CLIENT_ID = ""  # OAuth2 client ID
SERVICE_ACCOUNT_TOKEN_SECRET = ""  # OAuth2 client secret
```

### Mock Mode

For development and testing, a mock backend can be enabled:

```python
ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND = True
```

This simulates service account operations without requiring external backend connections.

## Backend API Requirements

When `SERVICE_ACCOUNT_USE_API` is enabled, the backend must implement the following endpoints:

### 1. Authentication Endpoint

**POST** `{SERVICE_ACCOUNT_TOKEN_URL}`

Request:

```text
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id={SERVICE_ACCOUNT_TOKEN_CLIENT_ID}
&client_secret={SERVICE_ACCOUNT_TOKEN_SECRET}
```

Response:

```json
{
    "access_token": "bearer-token-string"
}
```

### 2. Create Service Account

**POST** `{SERVICE_ACCOUNT_URL}`

Headers:

```text
Authorization: Bearer {access_token}
Content-Type: application/json
```

Request Body:

```json
{
    "email": "user@example.com",
    "description": "Service account description",
    "preferred_identifier": "optional-identifier",
    "scope_type": "project|customer",
    "scope_name": "Project/Customer name",
    "scope_uuid": "uuid-of-scope",
    "requester": {
        "username": "requesting-user",
        "email": "requester@example.com"
    }
}
```

Response:

```json
{
    "serviceAccount": {
        "status": "active",
        "username": "generated-username",
        "email": "user@example.com",
        "description": "Service account description",
        "unixUid": 1000,
        "unixGid": 1000,
        "scopeType": "project",
        "scopeName": "Project name",
        "scopeSlug": "project-slug",
        "owner": {
            "username": "owner-username",
            "email": "owner@example.com"
        }
    },
    "apiKey": {
        "apiKey": "generated-api-key",
        "createdAt": "2025-01-01T12:00:00Z",
        "expiresAt": "2025-02-01T12:00:00Z",
        "ttl": 2592000
    }
}
```

### 3. Update Service Account

**PUT** `{SERVICE_ACCOUNT_URL}/{username}`

Headers:

```text
Authorization: Bearer {access_token}
Content-Type: application/json
```

Request Body:

```json
{
    "email": "updated@example.com",
    "description": "Updated description"
}
```

### 4. Close Service Account

**PUT** `{SERVICE_ACCOUNT_URL}/{username}/close`

Headers:

```text
Authorization: Bearer {access_token}
```

Response:

```json
{
    "serviceAccount": {
        "status": "closed",
        "disabledDate": "2025-01-01T12:00:00Z"
    }
}
```

### 5. Rotate API Key

**PUT** `{SERVICE_ACCOUNT_URL}/{username}/rotate-api-key`

Headers:

```text
Authorization: Bearer {access_token}
```

Response:

```json
{
    "apiKey": {
        "apiKey": "new-generated-api-key",
        "createdAt": "2025-01-01T12:00:00Z",
        "expiresAt": "2025-02-01T12:00:00Z",
        "ttl": 2592000
    }
}
```

### 6. Get Service Account

**GET** `{SERVICE_ACCOUNT_URL}/{username}`

Headers:

```text
Authorization: Bearer {access_token}
```

Response: Same as create response structure

## API Endpoints (Frontend)

### Project Service Accounts

- **List**: `GET /api/marketplace-project-service-accounts/`
- **Create**: `POST /api/marketplace-project-service-accounts/`
- **Retrieve**: `GET /api/marketplace-project-service-accounts/{uuid}/`
- **Update**: `PATCH /api/marketplace-project-service-accounts/{uuid}/`
- **Delete**: `DELETE /api/marketplace-project-service-accounts/{uuid}/`
- **Rotate API Key**: `POST /api/marketplace-project-service-accounts/{uuid}/rotate_api_key/`

### Customer Service Accounts

- **List**: `GET /api/marketplace-customer-service-accounts/`
- **Create**: `POST /api/marketplace-customer-service-accounts/`
- **Retrieve**: `GET /api/marketplace-customer-service-accounts/{uuid}/`
- **Update**: `PATCH /api/marketplace-customer-service-accounts/{uuid}/`
- **Delete**: `DELETE /api/marketplace-customer-service-accounts/{uuid}/`
- **Rotate API Key**: `POST /api/marketplace-customer-service-accounts/{uuid}/rotate_api_key/`

## Permissions

Service account operations require the `MANAGE_SERVICE_ACCOUNT` permission at the appropriate scope:

- **Project Service Accounts**: Permission required at project or customer level
- **Customer Service Accounts**: Permission required at customer level

## Lifecycle Management

### Automatic Cleanup

Service accounts are automatically closed when their parent scope is deleted:

1. **Project Deletion**: All associated ProjectServiceAccounts are closed
2. **Customer Deletion**: All associated CustomerServiceAccounts are closed

This is handled by Django signal handlers:

- `close_service_accounts_on_project_deletion`
- `close_customer_service_accounts_on_customer_deletion`

### Account Limits

Organizations can enforce service account limits:

- **Project Level**: Set `max_service_accounts` on the Project model
- **Customer Level**: Set `max_service_accounts` on the Customer model

When limits are set, attempts to create accounts beyond the limit will be rejected with a validation error.

## Error Handling

Service accounts track errors through:

- **state**: Transitions to ERRED state on failures
- **error_message**: Human-readable error description
- **error_traceback**: Full error traceback for debugging

Failed operations automatically transition accounts to ERRED state, which can be recovered using `set_state_ok()` after resolving issues.

## Integration with GLAuth

Service accounts can be exported for GLAuth synchronization through the offering endpoint:

`GET /api/marketplace-offerings/{uuid}/glauth_users_config/`

This generates configuration records for:

- Offering users
- Robot accounts (including service accounts)

## Implementation Checklist

When implementing a service account backend, ensure:

- [ ] OAuth2 token endpoint is available and returns bearer tokens
- [ ] Service account creation endpoint generates unique usernames
- [ ] API keys are returned with expiration information
- [ ] Update operations modify only allowed fields (email, description)
- [ ] Close operation marks accounts as disabled
- [ ] Get operation returns current account status
- [ ] Rotate operation generates new API keys
- [ ] Error responses use standard HTTP status codes
- [ ] All endpoints validate bearer token authentication
- [ ] Account status transitions are logged appropriately

## Security Considerations

1. **API Keys**: Generated keys are returned only once during creation. Store securely.
2. **Token Expiration**: API keys should have reasonable TTL (default: 30 days)
3. **Permission Checks**: All operations validate user permissions at appropriate scope
4. **Audit Logging**: All service account operations are logged for audit trails
5. **Cleanup**: Accounts are automatically closed when parent resources are deleted

## Testing

Mock mode can be enabled for testing without external dependencies:

```python
from waldur_mastermind.marketplace import config
config.ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND = True
```

This simulates all backend operations locally, useful for:

- Unit tests
- Development environments
- Demo installations
