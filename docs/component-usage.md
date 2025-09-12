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

## Related Models

- **Resource**: The marketplace resource consuming components
- **OfferingComponent**: Defines component types and billing configuration
- **ResourcePlanPeriod**: Tracks billing plan changes over time
- **ComponentUserUsage**: Per-user usage tracking (separate model)
- **InvoiceItem**: Generated billing line items
