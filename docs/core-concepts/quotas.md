# Quotas Application

## Overview

The Quotas application is a Django app that provides generic implementation of quotas tracking functionality for Waldur:

1. Store and query resource limits and usages for project, customer or any other model
2. Aggregate quota usage in object hierarchies
3. Provide concurrent-safe quota updates using delta-based storage
4. Support multiple quota field types for different use cases

## Architecture

### Core Models

#### QuotaLimit

- Stores quota limit values for different scopes
- Uses generic foreign key to relate to any model instance
- Unique constraint on (name, content_type, object_id)
- Default value of -1 indicates unlimited quota

#### QuotaUsage

- Stores quota usage deltas instead of absolute values
- Enables concurrent updates without deadlocks
- Aggregated using SUM queries to get current usage
- Uses generic foreign key pattern for scope association

#### QuotaModelMixin

- Base mixin for models that need quota functionality
- Provides core quota management methods
- Defines abstract Quotas inner class for field definitions
- Includes property accessors for quotas, quota_usages, quota_limits

#### ExtendableQuotaModelMixin

- Extends QuotaModelMixin for runtime quota field addition
- Disables field caching to support dynamic fields
- Used when quota fields need to be added programmatically

### Quota Field Types

#### QuotaField

- Base quota field class
- Configurable default limits and backend flags
- Optional creation conditions for conditional quota assignment
- Supports callable default values: `QuotaField(default_limit=lambda scope: scope.attr)`

#### CounterQuotaField

- Automatically tracks count of target model instances
- Increases/decreases usage on target model creation/deletion
- Configurable delta calculation via `get_delta` function
- Example: `nc_resource_count` tracks total resources in a project

#### TotalQuotaField

- Aggregates sum of specific field values from target models
- Useful for tracking total storage size, RAM allocation, etc.
- Extends CounterQuotaField with field-specific aggregation
- Example: `nc_volume_size` sums all volume sizes in a project

#### UsageAggregatorQuotaField

- Aggregates quota usage from child objects with same quota name
- Enables hierarchical quota tracking (customer ← project ← resource)
- Configurable child quota name mapping
- Example: Customer's `nc_resource_count` aggregates from all projects

### Signal Handling and Automation

#### Quota Handlers

- `count_quota_handler_factory`: Creates handlers for CounterQuotaField automation
- `handle_aggregated_quotas`: Manages usage aggregation across hierarchies
- `get_ancestors`: Safely traverses object relationships with depth limits
- `delete_quotas_when_model_is_deleted`: Cleanup on model deletion

#### Signal Registration

- Automatically registers signals for CounterQuotaField instances
- Connects aggregation handlers to QuotaUsage model signals
- Handles project customer changes for quota recalculation

### Concurrency Safety

#### Delta-Based Storage

The quota system uses INSERT operations instead of UPDATE to avoid deadlocks:

- Usage deltas are stored in QuotaUsage records
- Current usage calculated via SUM aggregation
- Multiple concurrent requests can safely add usage deltas
- Prevents shared write deadlocks in high-concurrency scenarios

#### Transaction Safety

- `set_quota_usage` uses `@transaction.atomic` decorator
- Quota validation can be enabled per operation
- Safe quota changes through `apply_quota_usage` method

## Define Quota Fields

Models with quotas should inherit `QuotaModelMixin` and define a `Quotas` inner class:

```python
from waldur_core.quotas import models as quotas_models, fields as quotas_fields

class Tenant(quotas_models.QuotaModelMixin, models.Model):
    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        vcpu = quotas_fields.QuotaField(default_limit=20, is_backend=True)
        ram = quotas_fields.QuotaField(default_limit=51200, is_backend=True)
        storage = quotas_fields.QuotaField(default_limit=1024000, is_backend=True)
```

### Real-World Examples

#### Customer Quotas

```python
class Quotas(quotas_models.QuotaModelMixin.Quotas):
    enable_fields_caching = False
    nc_project_count = quotas_fields.CounterQuotaField(
        target_models=lambda: [Project],
        path_to_scope="customer",
    )
    nc_user_count = quotas_fields.QuotaField()
    nc_resource_count = quotas_fields.CounterQuotaField(
        target_models=lambda: BaseResource.get_all_models(),
        path_to_scope="project.customer",
    )
```

#### Project Quotas

```python
class Quotas(quotas_models.QuotaModelMixin.Quotas):
    enable_fields_caching = False
    nc_resource_count = quotas_fields.CounterQuotaField(
        target_models=lambda: BaseResource.get_all_models(),
        path_to_scope="project",
    )
```

## Quota Operations

### Basic Operations

- `get_quota_limit(quota_name)` - Get current limit (returns -1 for unlimited)
- `set_quota_limit(quota_name, limit)` - Set new quota limit
- `get_quota_usage(quota_name)` - Get current usage (SUM of deltas)
- `set_quota_usage(quota_name, usage)` - Set absolute usage value
- `add_quota_usage(quota_name, delta, validate=False)` - Add delta to usage

### Bulk Operations

- `apply_quota_usage(quota_deltas)` - Apply multiple quota deltas atomically
- `validate_quota_change(quota_deltas)` - Validate quota changes before applying

### Property Access

- `quotas` - List of all quotas with name, usage, limit
- `quota_usages` - Dictionary of current usage values
- `quota_limits` - Dictionary of current limit values

## Quota Validation

Use `validate_quota_change()` to check if quota changes would exceed limits:

```python
try:
    instance.validate_quota_change({'ram': 1024, 'storage': 2048})
except QuotaValidationError as e:
    # Handle quota exceeded error
    pass
```

## Shared Quota Resources

For resources that affect multiple quota scopes, implement `SharedQuotaMixin`:

```python
class MyResource(SharedQuotaMixin, models.Model):
    def get_quota_deltas(self):
        return {'storage': self.size, 'volumes': 1}

    def get_quota_scopes(self):
        return [self.project, self.tenant]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.increase_backend_quotas_usage(validate=True)
```

## Background Tasks

### Celery Tasks

- `update_custom_quotas()` - Triggers custom quota recalculation signal
- `update_standard_quotas()` - Recalculates all standard quota fields

These tasks enable periodic quota synchronization and can be scheduled via cron.

## Performance Considerations

### Hierarchy Traversal

- `get_ancestors()` includes depth limits (max_depth=10) to prevent infinite recursion
- Handles deletion scenarios gracefully with ObjectDoesNotExist catching
- Uses sets to eliminate duplicate ancestors in complex hierarchies

### Deletion Optimization

- Skips aggregation during bulk deletion (project deletion scenarios)
- Uses `_deleting` flag to avoid timeout issues
- Automatically cleans up quota records on model deletion

### Query Optimization

- Uses `Sum()` aggregation for efficient usage calculation
- Generic foreign keys enable single tables for all quota types
- Field caching can be disabled for dynamic quota scenarios

## Error Handling

### Exception Types

- `QuotaError` - Base quota system exception
- `QuotaValidationError` - Extends DRF ValidationError for quota limit violations

### Graceful Degradation

- Missing relationships during deletion are safely ignored
- Invalid scopes return empty quota collections
- Failed quota operations don't break primary workflows

## Integration Points

### Structure Integration

- Customer and Project models include standard quota definitions
- Project movement between customers triggers quota recalculation
- User count and resource count quotas are tracked automatically

### Plugin Integration

- `recalculate_quotas` signal allows plugin-specific quota logic
- Backend quota synchronization through plugin-specific handlers
- Resource-specific quota fields defined in individual plugins

## Usage Workflow

### Standard Workflow

1. **Quota Allocation**: Increase usage when resource allocation begins
2. **Validation**: Check quota limits before proceeding with operations
3. **Backend Sync**: Pull actual usage from backends periodically
4. **Cleanup**: Decrease usage only when backend deletion succeeds

### Error Recovery

- Frontend quota not modified if backend API calls fail
- Quota pulling (sync) handles discrepancies
- Manual recalculation available via management commands

## Sort Objects by Quotas

Inherit your `FilterSet` from `QuotaFilterMixin` and add quota ordering:

```python
class Meta:
    order_by = ['name', 'quotas__limit', '-quotas__limit']
```

Ordering can be done only by one quota at a time.
