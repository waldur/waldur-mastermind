# ComponentUsage Model Documentation

## Overview

The `ComponentUsage` model tracks detailed usage data for resource components in the Waldur marketplace system. It provides comprehensive consumption tracking with billing period and plan period associations, supporting both one-time and recurring usage patterns.

## Model Definition

**File**: `src/waldur_mastermind/marketplace/models.py:1835`

```python
class ComponentUsage(
    TimeStampedModel,
    core_models.DescribableMixin,
    core_models.BackendMixin,
    core_models.UuidMixin,
    LoggableMixin,
):
```

## Fields

| Field | Type | Description |
|-------|------|-------------|
| `resource` | ForeignKey(Resource) | The resource this usage belongs to |
| `component` | ForeignKey(OfferingComponent) | The component being tracked |
| `usage` | DecimalField | Usage amount (max_digits=20, decimal_places=2) |
| `date` | DateTimeField | When the usage occurred |
| `plan_period` | ForeignKey(ResourcePlanPeriod) | Associated billing plan period (optional) |
| `billing_period` | DateField | Billing period for this usage |
| `recurring` | BooleanField | Whether value reuses monthly until changed |
| `modified_by` | ForeignKey(User) | User who last modified this record |

## Relationships

### Required Relationships

- **Resource**: Links to the marketplace resource being consumed
- **OfferingComponent**: Links to the specific component type (CPU, RAM, storage, etc.)

### Optional Relationships

- **ResourcePlanPeriod**: Associates usage with specific billing plan timeframes
- **User**: Tracks who modified the usage record

## Constraints

The model enforces uniqueness through two constraints:

1. **With plan_period**: Unique combination of `[resource, component, plan_period, billing_period]`
2. **Without plan_period**: Unique combination of `[resource, component, billing_period]` when `plan_period` is NULL

## Creating ComponentUsage Records

### 1. Direct Creation

```python
from waldur_mastermind.marketplace import models
from django.utils import timezone
from waldur_core.core import utils as core_utils

# Create usage record
usage = models.ComponentUsage.objects.create(
    resource=resource,
    component=offering_component,
    usage=100.50,
    date=timezone.now(),
    billing_period=core_utils.month_start(timezone.now()),
    plan_period=plan_period,  # optional
    recurring=False
)
```

### 2. Via Factory (Testing)

```python
from waldur_mastermind.marketplace.tests import factories

# Create test usage
usage = factories.ComponentUsageFactory(
    resource=resource,
    component=component,
    usage=50.0
)
```

### 3. Via API Endpoint

**POST** `/api/marketplace-component-usages/set_usage/`

```json
{
    "resource": "resource-uuid",
    "component": "component-uuid",
    "usage": 100.50,
    "date": "2024-01-15T10:30:00Z",
    "recurring": false
}
```

### 4. Via Utility Function

```python
from waldur_mastermind.marketplace import utils

# Import current usages from resource
utils.import_current_usages(resource)
```

## Usage Patterns

### Automatic Usage Import

The `import_current_usages()` function automatically creates/updates ComponentUsage records:

**File**: `src/waldur_mastermind/marketplace/utils.py:2273`

```python
def import_current_usages(resource):
    date = datetime.date.today()
    for component_type, component_usage in resource.current_usages.items():
        # Get or create ComponentUsage with max() logic
        # Updates existing record with higher usage value
```

### Get-or-Update Pattern

The system uses a get-or-update pattern that preserves the maximum usage value:

```python
try:
    component_usage_object = models.ComponentUsage.objects.get(
        resource=resource,
        component=offering_component,
        billing_period=core_utils.month_start(date),
        plan_period=plan_period,
    )
    # Keep maximum usage value
    component_usage_object.usage = max(
        component_usage, component_usage_object.usage
    )
    component_usage_object.save()
except models.ComponentUsage.DoesNotExist:
    # Create new record
    models.ComponentUsage.objects.create(...)
```

### Recurring Usage

Set `recurring=True` for usage that should be automatically carried forward each month until changed:

```python
usage = models.ComponentUsage.objects.create(
    resource=resource,
    component=component,
    usage=100.0,
    recurring=True,  # Will be reused monthly
    # ... other fields
)
```

## API Access

### ViewSet

**Class**: `ComponentUsageViewSet`
**File**: `src/waldur_mastermind/marketplace/views.py:2840`

- **Endpoint**: `/api/marketplace-component-usages/`
- **Permissions**: Generic role filter + structure permissions
- **Actions**: Read-only + `set_usage` action
- **Filtering**: By resource, component, date ranges, etc.

### Serializer

**Class**: `ComponentUsageSerializer`
**File**: `src/waldur_mastermind/marketplace/serializers.py:2200`

Includes read-only fields for related objects:

- `resource_name`, `resource_uuid`
- `offering_name`, `offering_uuid`
- `project_name`, `project_uuid`
- `customer_name`, `customer_uuid`

## Billing Integration

ComponentUsage records integrate with the billing system through:

1. **ResourcePlanPeriod**: Associates usage with specific billing plans
2. **billing_period**: Groups usage by month for invoice generation
3. **Invoice Items**: Usage data flows into invoice line items

The `get_invoice_item_for_component_usage()` function links usage records to invoice items for billing purposes.

## Testing

### Factories Available

```python
from waldur_mastermind.marketplace.tests.factories import (
    ComponentUsageFactory,
    ResourceFactory,
    OfferingComponentFactory,
    ResourcePlanPeriodFactory
)
```

### Test Examples

See `src/waldur_mastermind/marketplace/tests/test_usage.py` for comprehensive test examples including:

- Usage filtering by date ranges
- Plan period associations
- API endpoint testing
- Billing integration tests

## ComponentUserUsage Model

### ComponentUserUsage Overview

The `ComponentUserUsage` model tracks component usage on a per-user basis, providing detailed consumption data for individual users within resources. It works in conjunction with `ComponentUsage` to provide granular user-level usage tracking and analysis.

### ComponentUserUsage Model Definition

**File**: `src/waldur_mastermind/marketplace/models.py:1880`

```python
class ComponentUserUsage(
    TimeStampedModel,
    core_models.DescribableMixin,
    core_models.BackendMixin,
    core_models.UuidMixin,
    LoggableMixin,
):
```

### ComponentUserUsage Fields

| Field | Type | Description |
|-------|------|-------------|
| `user` | ForeignKey(OfferingUser) | The offering user (optional) |
| `username` | CharField | Username string (max_length=100) |
| `component_usage` | ForeignKey(ComponentUsage) | Associated component usage record |
| `usage` | DecimalField | User-specific usage amount (max_digits=20, decimal_places=2) |

### ComponentUserUsage Relationships

#### ComponentUserUsage Required Relationships

- **ComponentUsage**: Links to the parent component usage record

#### ComponentUserUsage Optional Relationships

- **OfferingUser**: Links to the specific offering user account (can be null)

### ComponentUserUsage Constraints

The model enforces uniqueness through:

- **Unique together**: `("username", "component_usage")` - prevents duplicate user usage for same component usage record

### Creating ComponentUserUsage Records

### 1. Via Update-or-Create Pattern

The most common creation pattern uses `update_or_create()`:

```python
from waldur_mastermind.marketplace import models

# Sync user usage from external system
component_user_usage, created = models.ComponentUserUsage.objects.update_or_create(
    username=username,
    component_usage=component_usage,
    defaults={
        "usage": usage_amount,
        "user": offering_user  # optional
    }
)
```

### 2. Via Utility Function

**File**: `src/waldur_mastermind/marketplace/utils.py:2300`

```python
from waldur_mastermind.marketplace.utils import sync_component_user_usage

# Synchronize user usage from allocation
sync_component_user_usage(allocation_user_usage, plugin_name)
```

### 3. Direct Creation

```python
# Create user usage record directly
user_usage = models.ComponentUserUsage.objects.create(
    username="john_doe",
    component_usage=component_usage,
    usage=25.50,
    user=offering_user  # optional
)
```

### ComponentUserUsage Usage Patterns

#### Automatic Synchronization

The `sync_component_user_usage()` function automatically creates/updates user usage records from allocation data:

```python
def sync_component_user_usage(allocation_user_usage, plugin_name):
    # Find associated resource and component
    # Iterate through offering components
    # Create/update ComponentUserUsage records
    component_user_usage, created = (
        models.ComponentUserUsage.objects.update_or_create(
            username=allocation_user_usage.username,
            component_usage=component_usage,
            defaults={"usage": usage, "user": offering_user},
        )
    )
```

#### Username vs User Relationship

- **username**: Always required, stores the string username
- **user**: Optional, links to `OfferingUser` record when available
- This dual approach supports scenarios where user accounts may not exist in the system yet

### ComponentUserUsage API Access

#### ComponentUserUsage ViewSet

**Class**: `ComponentUserUsageViewSet`
**File**: `src/waldur_mastermind/marketplace/views.py:2850`

- **Endpoint**: `/api/marketplace-component-user-usages/`
- **Permissions**: Generic role filter + structure permissions
- **Actions**: Read-only operations
- **Filtering**: By component usage, user, username, etc.

#### ComponentUserUsage Serializer

**Class**: `ComponentUserUsageSerializer`
**File**: `src/waldur_mastermind/marketplace/serializers.py:2120`

Key fields exposed:

- `user`: Hyperlinked to OfferingUser
- `component_usage`: Hyperlinked to ComponentUsage
- `measured_unit`: Read-only from component
- `username`, `usage`, `created`, etc.

### ComponentUserUsage User Limits

#### ComponentUserUsageLimit Model

A related model `ComponentUserUsageLimit` defines usage limits per user:

```python
class ComponentUserUsageLimit(
    TimeStampedModel,
    core_models.UuidMixin,
    LoggableMixin,
):
    resource = models.ForeignKey(Resource)
    component = models.ForeignKey(OfferingComponent)
    user = models.ForeignKey(OfferingUser)
    limit = models.DecimalField()
```

**Constraints**: Unique together `("resource", "component", "user")`

#### Usage Validation

The system validates user usage against limits during creation:

```python
# Check usage limits during validation
usage_limit = models.ComponentUserUsageLimit.objects.filter(
    resource=component_usage.resource,
    component=component_usage.component,
    user=user
).first()

if usage_limit and new_usage > usage_limit.limit:
    raise ValidationError("Usage exceeds limit")
```

### ComponentUserUsage Integration Patterns

#### SLURM Plugin Integration

ComponentUserUsage is commonly used with SLURM allocations:

```python
# Sync from SLURM allocation data
for offering_component in models.OfferingComponent.objects.filter(offering=resource.offering):
    if hasattr(allocation_user_usage, offering_component.type + "_usage"):
        usage = getattr(allocation_user_usage, offering_component.type + "_usage")
        # Create ComponentUserUsage record
```

#### Remote Plugin Integration

Remote plugins use ComponentUserUsage for external system synchronization:

**File**: `src/waldur_mastermind/marketplace_remote/tasks.py`

### ComponentUserUsage Testing

#### ComponentUserUsage Test Examples

See `src/waldur_mastermind/marketplace/tests/test_usage.py` for test examples including:

- User usage creation and validation
- Limit enforcement testing
- API endpoint testing
- Integration with ComponentUsage

#### Factory Usage

While there's no dedicated factory, you can create test records:

```python
from waldur_mastermind.marketplace.tests import factories

# Create related objects first
component_usage = factories.ComponentUsageFactory()
offering_user = factories.OfferingUserFactory()

# Create user usage
user_usage = models.ComponentUserUsage.objects.create(
    username="test_user",
    component_usage=component_usage,
    usage=10.0,
    user=offering_user
)
```

### ComponentUserUsage Related Models

- **ComponentUsage**: Parent usage record this user usage belongs to
- **OfferingUser**: User account within the offering context
- **ComponentUserUsageLimit**: Per-user usage limits
- **Resource**: The marketplace resource (via ComponentUsage)
- **OfferingComponent**: Component type (via ComponentUsage)

## ComponentUsage and ComponentUserUsage Relationship

### Summary

- **ComponentUsage**: Tracks total component usage for a resource
- **ComponentUserUsage**: Breaks down usage by individual users
- **Relationship**: ComponentUserUsage → ComponentUsage (many-to-one)
- **Use Case**: Detailed user-level billing and usage analysis
