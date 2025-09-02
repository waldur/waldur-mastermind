# Ticket-Based Offerings

Ticket-based offerings integrate Waldur marketplace with external ticketing systems (JIRA, SMAX, Zammad) to manage service provisioning through support tickets.

## Overview

When offerings are configured with `type = "Marketplace.Support"`, orders are processed through external ticketing systems rather than direct API provisioning. This enables:

- Manual service provisioning workflows
- Integration with existing ITSM processes
- Human approval and intervention
- Complex provisioning that requires multiple steps

## Architecture

```mermaid
graph LR
    Order[Marketplace Order] --> Issue[Support Issue]
    Issue --> Backend[Ticketing Backend]
    Backend --> Status[Status Change]
    Status --> Callback[Resource Callback]
    Callback --> Resource[Resource State]
```

## Order Processing Flow

### 1. Order Creation

When a customer creates an order for a ticket-based offering:

- A support issue is created in the configured backend (JIRA/SMAX/Zammad)
- The order is linked to the issue via `issue.resource`
- The issue contains order details in its description

### 2. Status Synchronization

The system monitors issue status changes through:

- Backend synchronization (`sync_issues()`)
- Webhooks (JIRA, SMAX, Zammad - if configured)
- Periodic polling

### 3. Callback Triggering

When an issue status changes, the system determines the appropriate callback based on:

- **Order Type** (CREATE, UPDATE, TERMINATE)
- **Resolution Status** (resolved, canceled)

The callback mapping is defined in `marketplace_support/handlers.py`:

```python
RESOURCE_CALLBACKS = {
    (ItemTypes.CREATE, True): callbacks.resource_creation_succeeded,
    (ItemTypes.CREATE, False): callbacks.resource_creation_canceled,
    (ItemTypes.UPDATE, True): callbacks.resource_update_succeeded,
    (ItemTypes.UPDATE, False): callbacks.resource_update_failed,
    (ItemTypes.TERMINATE, True): callbacks.resource_deletion_succeeded,
    (ItemTypes.TERMINATE, False): callbacks.resource_deletion_failed,
}
```

## Issue Resolution Detection

The system determines if an issue is resolved or canceled through the `IssueStatus` model:

### IssueStatus Configuration

Each status name from the ticketing system maps to one of two types:

- `IssueStatus.Types.RESOLVED` - Successfully completed
- `IssueStatus.Types.CANCELED` - Failed or canceled

Example configuration:

```python
# In the database/admin:
IssueStatus.objects.create(name="Done", type=IssueStatus.Types.RESOLVED)
IssueStatus.objects.create(name="Rejected", type=IssueStatus.Types.CANCELED)
IssueStatus.objects.create(name="Canceled", type=IssueStatus.Types.CANCELED)
```

### Resolution Logic

1. When an issue's status changes (e.g., from backend sync)
2. The `issue.resolved` property is evaluated:
  - Looks up the status name in `IssueStatus` table
  - Returns `True` if type is `RESOLVED`
  - Returns `False` if type is `CANCELED`
  - Returns `None` for other statuses

3. Based on `(order.type, issue.resolved)` combination, the appropriate callback is triggered

## Resource Deletion Failed Scenario

The `resource_deletion_failed` callback is triggered when:

1. **Issue Status Changes**: The support ticket's status is updated
2. **Order Type is TERMINATE**: The order represents a resource deletion request
3. **Status Maps to CANCELED**: The new status is configured as `IssueStatus.Types.CANCELED`
4. **Callback Execution**: `callbacks.resource_deletion_failed(order.resource)` is called

This typically happens when:

- Support staff reject a deletion request
- Technical issues prevent resource removal
- Business rules block the deletion
- The request is canceled before completion

## Configuration

### Backend Setup

Configure the ticketing backend in settings:

```python
WALDUR_SUPPORT = {
    'BACKEND': 'waldur_mastermind.support.backend.smax.SmaxBackend',
    # or 'waldur_mastermind.support.backend.atlassian.ServiceDeskBackend'
    # or 'waldur_mastermind.support.backend.zammad.ZammadBackend'
}
```

### Status Mapping

Ensure IssueStatus objects are configured to map backend statuses correctly:

```python
# Admin interface or data migration
resolved_statuses = ["Done", "Resolved", "Completed"]
canceled_statuses = ["Rejected", "Canceled", "Failed", "Won't Do"]

for status in resolved_statuses:
    IssueStatus.objects.get_or_create(
        name=status,
        defaults={'type': IssueStatus.Types.RESOLVED}
    )

for status in canceled_statuses:
    IssueStatus.objects.get_or_create(
        name=status,
        defaults={'type': IssueStatus.Types.CANCELED}
    )
```

## Best Practices

1. **Status Configuration**: Ensure all possible backend statuses are mapped in IssueStatus
2. **Monitoring**: Regularly sync issues to detect status changes
3. **Error Handling**: Implement proper error handling in callbacks
4. **Logging**: Monitor handler execution through logs for debugging
5. **Testing**: Test status transitions with different order types

## Troubleshooting

### Callbacks Not Firing

- Check if IssueStatus entries exist for the backend's status values
- Verify the offering type is set to `"Marketplace.Support"`
- Ensure issue synchronization is running
- Check logs for handler execution

### Wrong Callback Triggered

- Review IssueStatus type configuration
- Verify the order type (CREATE/UPDATE/TERMINATE)
- Check the issue resolution logic in logs

### Missing Status Mappings

If you see critical log messages about missing statuses:

```text
"There is no information about statuses of an issue. Please, add resolved and canceled statuses in admin."
```

Add the required IssueStatus entries through the admin interface.
