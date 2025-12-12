# User Actions Notification System

The User Actions system provides a framework for detecting and managing user-specific actions across Waldur components. It helps users stay informed about items requiring attention, such as pending orders, expiring resources, and stale assets.

## Core Features

- **Action Detection**: Automated discovery of user-specific actions across applications
- **Urgency Classification**: Three-tier urgency system (low, medium, high)
- **Action Management**: Users can silence actions temporarily or permanently
- **Corrective Actions**: Predefined actions users can take to resolve issues
- **Bulk Operations**: Bulk silence multiple actions based on filters
- **API Execution**: Execute corrective actions directly through API endpoints
- **Admin Controls**: Administrative endpoints for triggering action updates
- **Audit Trail**: Complete execution history for actions taken
- **OpenAPI Documentation**: Fully documented API with drf-spectacular integration

## Architecture

### Provider Framework

Action providers inherit from `BaseActionProvider` and implement:

- `get_actions_for_user(user)` - Returns user-specific actions
- `get_affected_users()` - Returns users who might have actions
- `get_corrective_actions(user, obj)` - Returns available corrective actions

### Database Models

- `UserAction` - Individual action items with urgency, due dates, silencing support, and corrective actions
- `UserActionExecution` - Audit trail for executed actions with success/failure tracking
- `UserActionProvider` - Registry of registered providers with execution status and scheduling

### API Endpoints

#### User Action Management

- `GET /api/user-actions/` - List user actions (filterable by urgency, type, silenced status)
- `GET /api/user-actions/{uuid}/` - Get specific action details
- `GET /api/user-actions/summary/` - Action statistics and counts by urgency and type
- `POST /api/user-actions/{uuid}/silence/` - Silence action temporarily or permanently
- `POST /api/user-actions/{uuid}/unsilence/` - Remove silence from an action
- `POST /api/user-actions/{uuid}/execute_action/` - Execute corrective actions
- `POST /api/user-actions/bulk_silence/` - Bulk silence actions based on filters
- `POST /api/user-actions/update_actions/` - Trigger action update (admin only)

#### Execution History

- `GET /api/user-action-executions/` - View action execution history

#### Provider Management (Admin Only)

- `GET /api/user-action-providers/` - List registered action providers

## Marketplace Providers

Two providers are included for marketplace workflows:

### PendingOrderProvider

Detects orders pending consumer approval for more than 24 hours. Provides corrective actions:

- View order details
- Approve order (API endpoint)
- Reject order
- Contact customer

### ExpiringResourceProvider

Finds resources expiring within 30 days. Corrective actions include:

- View resource details
- Extend resource
- Create backup
- Terminate early
- Migrate to new resource

## Configuration

```python
WALDUR_USER_ACTIONS = {
    'ENABLED': True,
    'UPDATE_SCHEDULE': '0 */6 * * *',  # Every 6 hours
    'MAX_ACTIONS_PER_USER': 100,
    'DEFAULT_SILENCE_DURATION_DAYS': 7,
    'NOTIFICATION_ENABLED': True,
    'NOTIFICATION_SCHEDULE': '0 9 * * *',  # Daily at 9 AM
    'HIGH_URGENCY_NOTIFICATION_THRESHOLD': 1,
}
```

## Creating Custom Providers

1. Create a provider class inheriting from `BaseActionProvider`
2. Implement required methods
3. Register with `register_provider(YourProvider)`
4. Create `user_actions.py` in your app to auto-register on startup

Example provider structure:

```python
from waldur_core.user_actions.providers import (
    BaseActionProvider, ActionCategory, ActionSeverity,
    CorrectiveAction, register_provider
)

class MyActionProvider(BaseActionProvider):
    action_type = "my_action"
    display_name = "My Actions"

    def get_actions_for_user(self, user):
        return [{
            'title': 'Action Title',
            'description': 'Description',
            'urgency': 'medium',
            'due_date': some_date,
            'action_url': '/path/to/action/',
            'related_object': model_instance,
        }]

register_provider(MyActionProvider)
```

## Automated Tasks

Celery tasks run periodically:

- **Action Updates**: Every 6 hours - detect new actions
- **Cleanup**: Daily/weekly - remove expired silenced actions and old executions
- **Notifications**: Daily at 9 AM - send action digest emails

## API Usage Examples

### Listing Actions

List all actions for current user:

```bash
GET /api/user-actions/
```

List high-urgency actions:

```bash
GET /api/user-actions/?urgency=high
```

List actions including silenced ones:

```bash
GET /api/user-actions/?include_silenced=true
```

Filter by action type:

```bash
GET /api/user-actions/?action_type=pending_order
```

### Action Summary

Get action statistics:

```bash
GET /api/user-actions/summary/
```

Response example:

```json
{
  "total": 5,
  "overdue": 2,
  "by_urgency": {
    "high": 2,
    "medium": 2,
    "low": 1
  },
  "by_type": {
    "pending_order": 3,
    "expiring_resource": 2
  }
}
```

### Silencing Actions

Silence an action permanently:

```bash
POST /api/user-actions/{uuid}/silence/
{}
```

Silence an action for 7 days:

```bash
POST /api/user-actions/{uuid}/silence/
{"duration_days": 7}
```

Remove silence from an action:

```bash
POST /api/user-actions/{uuid}/unsilence/
```

Bulk silence high-urgency actions for 14 days:

```bash
POST /api/user-actions/bulk_silence/?urgency=high
{"duration_days": 14}
```

### Executing Corrective Actions

Execute a specific corrective action:

```bash
POST /api/user-actions/{uuid}/execute_action/
{"action_label": "Approve order"}
```

### Administrative Actions

Trigger action update for all providers (admin only):

```bash
POST /api/user-actions/update_actions/
{}
```

Trigger update for specific provider (admin only):

```bash
POST /api/user-actions/update_actions/
{"provider_action_type": "pending_order"}
```

### Viewing Execution History

Get execution history for current user:

```bash
GET /api/user-action-executions/
```

### Managing Providers (Admin Only)

List all registered providers:

```bash
GET /api/user-action-providers/
```

## Security and Permissions

- **User Actions**: Users can only view and manage their own actions
- **Execution History**: Users can only view their own execution history
- **Provider Management**: Requires admin permissions
- **Update Actions**: Requires admin permissions
- **Corrective Actions**: Subject to individual action permission requirements

## OpenAPI Documentation

All endpoints are fully documented with OpenAPI specifications via drf-spectacular:

- Request/response schemas are automatically generated
- Interactive API documentation available at `/api/docs/`
- Proper error response documentation for all endpoints
- Examples and validation rules included

The system integrates with existing Waldur permissions and follows established patterns for extensibility across all Waldur applications.
