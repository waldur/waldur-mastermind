# User Actions Notification System

The User Actions system provides a framework for detecting and managing user-specific actions across Waldur components. It helps users stay informed about items requiring attention, such as pending orders, expiring resources, and stale assets.

## Core Features

- **Action Detection**: Automated discovery of user-specific actions across applications
- **Urgency Classification**: Three-tier urgency system (low, medium, high)
- **Action Management**: Users can silence actions temporarily or permanently
- **Corrective Actions**: Predefined actions users can take to resolve issues
- **Audit Trail**: Complete execution history for actions taken

## Architecture

### Provider Framework

Action providers inherit from `BaseActionProvider` and implement:

- `get_actions_for_user(user)` - Returns user-specific actions
- `get_affected_users()` - Returns users who might have actions
- `get_corrective_actions(user, obj)` - Returns available corrective actions

### Database Models

- `UserAction` - Individual action items with urgency, due dates, and silencing
- `UserActionExecution` - Audit trail for executed actions
- `UserActionProvider` - Registry of registered providers

### API Endpoints

- `GET /api/user-actions/` - List user actions (filterable by urgency)
- `GET /api/user-actions/summary/` - Action statistics and counts
- `POST /api/user-actions/{uuid}/silence/` - Silence actions
- `POST /api/user-actions/{uuid}/execute_action/` - Execute corrective actions

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

## Usage Examples

List high-urgency actions:

```bash
GET /api/user-actions/?urgency=high
```

Get action summary:

```bash
GET /api/user-actions/summary/
# Returns: {"total": 5, "overdue": 2, "by_urgency": {"high": 2, "medium": 2, "low": 1}}
```

Silence an action for 7 days:

```bash
POST /api/user-actions/{uuid}/silence/
{"duration_days": 7}
```

The system integrates with existing Waldur permissions and follows established patterns for extensibility across all Waldur applications.
