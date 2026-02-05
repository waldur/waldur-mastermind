# Billing and Invoicing

## Overview

Waldur's billing system creates invoice items for marketplace resources based on their offering component's billing type. The central orchestrator is `MarketplaceBillingService` (`src/waldur_mastermind/marketplace/billing.py`), which dispatches to specialized processors depending on the billing type.

## Billing Types

Defined in `BillingTypes` (`src/waldur_mastermind/marketplace/enums.py`):

| Type | Value | Trigger | Recurrence | Handler |
|------|-------|---------|------------|---------|
| FIXED | `"fixed"` | Resource activation | Monthly (prorated) | `MarketplaceBillingService` |
| USAGE | `"usage"` | Usage report submission | Per report | `BillingUsageProcessor` |
| ONE_TIME | `"one"` | Resource creation | Once | `MarketplaceBillingService` |
| ON_PLAN_SWITCH | `"few"` | Plan change | Once per switch | `MarketplaceBillingService` |
| LIMIT | `"limit"` | Resource creation / limit change | Varies by `limit_period` | `LimitPeriodProcessor` |

## Billing Type Dispatch

```mermaid
graph TD
    A[Resource event] --> B{Billing type?}
    B -->|FIXED| C[Create prorated monthly item]
    B -->|ONE_TIME| D{Order type = CREATE?}
    D -->|Yes| E[Create single charge]
    D -->|No| F[Skip]
    B -->|ON_PLAN_SWITCH| G{Order type = UPDATE?}
    G -->|Yes| H[Create single charge]
    G -->|No| I[Skip]
    B -->|USAGE| J[Skip - handled by BillingUsageProcessor]
    B -->|LIMIT| K[LimitPeriodProcessor]
    K --> L{limit_period?}
    L -->|MONTH| M[Monthly invoice item]
    L -->|QUARTERLY| N[Quarterly invoice item]
    L -->|ANNUAL| O[Annual invoice item]
    L -->|TOTAL| P[One-time quantity item]
```

## Limit Periods

For components with `billing_type=LIMIT`, the `limit_period` field on `OfferingComponent` controls when and how invoice items are created.

Defined in `LimitPeriods` (`src/waldur_mastermind/marketplace/enums.py`):

| Period | Value | Invoice creation | Billing window | Unit |
|--------|-------|-----------------|----------------|------|
| MONTH | `"month"` | Every month | 1st to end of month | Plan unit |
| QUARTERLY | `"quarterly"` | Months 1, 4, 7, 10 only | Quarter start to quarter end (e.g., Jan 1 - Mar 31) | Plan unit |
| ANNUAL | `"annual"` | Resource's creation anniversary month | 12 months from delivery date | Plan unit |
| TOTAL | `"total"` | Once on creation; incremental on changes | Full resource lifetime | QUANTITY |

### Quarterly Billing Timeline

```mermaid
sequenceDiagram
    participant Jan as January
    participant Feb as February
    participant Mar as March
    participant Apr as April

    Note over Jan: Q1 billing month
    Jan->>Jan: Create invoice item (Jan 1 - Mar 31)
    Note over Feb: Not a billing month for quarterly
    Feb->>Feb: Skip (no new item)
    Note over Feb: Limit changes update Jan invoice item
    Feb-->>Jan: Update existing Q1 item with split periods
    Note over Mar: Not a billing month for quarterly
    Mar->>Mar: Skip (no new item)
    Note over Apr: Q2 billing month
    Apr->>Apr: Create invoice item (Apr 1 - Jun 30)
```

## Invoice Lifecycle

### Invoice States

| State | Description |
|-------|-------------|
| PENDING | Active invoice for current billing period. Items can be added/modified. |
| PENDING_FINALIZATION | Transitional state used when a grace period is configured. Items can still be added/modified. |
| CREATED | Finalized invoice. Items are frozen. |
| PAID | Invoice has been paid. |
| CANCELED | Invoice has been canceled. |

Both PENDING and PENDING_FINALIZATION are considered **mutable states** — invoice items can be added or updated while the invoice is in either state.

### Monthly Invoice Creation

The `create_monthly_invoices` task (`src/waldur_mastermind/invoices/tasks.py`) runs at midnight on the 1st of each month:

1. Previous month PENDING invoices are finalized (see Finalization below)
2. For each customer, `MarketplaceBillingService.get_or_create_invoice` is called
3. If the invoice is newly created, all active billable resources are processed via `_process_resource`

When a resource is activated mid-month, `_register` calls `get_or_create_invoice`. If the invoice already exists, it adds items for just that resource with prorated start/end dates.

### Invoice Finalization

Finalization transitions invoices from mutable to immutable (CREATED) state. The behavior depends on the `INVOICE_FINALIZATION_GRACE_PERIOD_HOURS` setting:

**Without grace period** (default, `grace_hours = 0`):

1. On the 1st at midnight, `create_monthly_invoices` finalizes previous month invoices immediately
2. Overdue credits are zeroed, compensations are applied, invoices transition PENDING → CREATED
3. Reports and notifications are sent

**With grace period** (e.g., `grace_hours = 24`):

1. On the 1st at midnight, `create_monthly_invoices` transitions previous month invoices to PENDING_FINALIZATION
2. The `finalize_previous_invoices` task runs hourly on the 1st–3rd of each month
3. Once the configured grace period has elapsed (measured from midnight on the 1st), it finalizes: PENDING_FINALIZATION → CREATED
4. Reports and notifications are sent only after all invoices are finalized

The grace period allows late usage data (e.g., from external billing systems) to be captured before invoices are frozen.

```mermaid
graph TD
    A[1st of month, midnight] --> B{Grace period configured?}
    B -->|No| C[PENDING → CREATED immediately]
    C --> D[Send reports & notifications]
    B -->|Yes| E[PENDING → PENDING_FINALIZATION]
    E --> F[Hourly check: grace period elapsed?]
    F -->|No| G[Skip, retry next hour]
    F -->|Yes| H[PENDING_FINALIZATION → CREATED]
    H --> I{All invoices finalized?}
    I -->|No| J[Wait for next hourly run]
    I -->|Yes| D
```

### Credits and Compensations

Customer credits can be configured with an optional `end_date` (must be the 1st of a month). During invoice finalization:

1. **Overdue credits are zeroed**: Credits with `end_date` before the effective date are set to zero
2. **Compensations are applied**: `MonthlyCompensation` calculates and applies credit-based discounts to invoice items
3. **Expected consumption is updated**: Linear consumption projections are recalculated

When a grace period is used, the effective date for zeroing credits is always the 1st of the current month (not the actual finalization date). This ensures credits with `end_date` on the 1st are still applied to the previous month's invoice before being zeroed.

### Configuration

The grace period is configured in `WALDUR_INVOICES` settings:

```python
WALDUR_INVOICES = {
    # Grace period in hours before finalizing previous month invoices.
    # 0 means finalize immediately (default, backward compatible).
    # When > 0, invoices transition PENDING -> PENDING_FINALIZATION on the 1st,
    # then PENDING_FINALIZATION -> CREATED after this many hours.
    "INVOICE_FINALIZATION_GRACE_PERIOD_HOURS": 0,
}
```

## Handling Limit Changes

The `post_save` signal on `Resource` triggers `process_billing_on_resource_save` (`src/waldur_mastermind/marketplace/handlers.py`), which calls `MarketplaceBillingService.handle_limits_change` when `resource.limits` changes.

```mermaid
graph TD
    A[resource.limits changed] --> B[handle_limits_change]
    B --> C{For each limit component}
    C --> D{limit_period?}
    D -->|MONTH / QUARTERLY / ANNUAL| E[_create_or_update_invoice_item]
    D -->|TOTAL| F[_create_invoice_item_for_total_limit]

    E --> G{Invoice item exists<br>for this component?}
    G -->|Yes| H[_update_invoice_item:<br>Split resource_limit_periods]
    G -->|No, periodic| I{Check billing period<br>origin invoice}
    I -->|Found on origin invoice| H
    I -->|Not found| J[Create new invoice item]

    F --> K[Calculate diff from<br>all previous items]
    K --> L{diff = 0?}
    L -->|Yes| M[Skip]
    L -->|No| N[Create incremental item<br>positive or negative price]
```

### Periodic Limit Updates (MONTH, QUARTERLY, ANNUAL)

When a limit changes for a periodic component, `_update_invoice_item` splits the existing invoice item's `resource_limit_periods` into old and new segments with date boundaries. The total quantity is recalculated as the sum across all periods.

For QUARTERLY and ANNUAL components, the system looks for the invoice item on the billing period's original invoice (e.g., the January invoice for a Q1 change happening in February), not just the current month's invoice.

Example: A quarterly component with limit changed from 100 to 150 on February 15th updates the January invoice item's `resource_limit_periods`:

```json
[
  {"start": "2025-01-01T00:00:00", "end": "2025-02-15T23:59:59", "quantity": 100},
  {"start": "2025-02-16T00:00:00", "end": "2025-03-31T23:59:59", "quantity": 150}
]
```

### TOTAL Limit Updates

For TOTAL period components, the system:

1. Sums all previously billed quantities (accounting for negative/compensation items)
2. Calculates the difference between the new limit and the total already billed
3. Creates a new incremental invoice item for the difference (with negative `unit_price` for decreases)

## Key Source Files

| File | Class/Function | Purpose |
|------|---------------|---------|
| `src/waldur_mastermind/marketplace/billing.py` | `MarketplaceBillingService` | Central billing orchestrator |
| `src/waldur_mastermind/marketplace/billing_limit.py` | `LimitPeriodProcessor` | LIMIT billing type logic |
| `src/waldur_mastermind/marketplace/billing_usage.py` | `BillingUsageProcessor` | USAGE billing type logic |
| `src/waldur_mastermind/marketplace/handlers.py` | `process_billing_on_resource_save` | Signal handler for resource changes |
| `src/waldur_mastermind/invoices/tasks.py` | `create_monthly_invoices` | Monthly invoice creation task |
| `src/waldur_mastermind/invoices/tasks.py` | `finalize_previous_invoices` | Deferred invoice finalization (grace period) |
| `src/waldur_mastermind/invoices/compensations.py` | `MonthlyCompensation` | Credit-based compensation logic |
| `src/waldur_mastermind/marketplace/enums.py` | `BillingTypes`, `LimitPeriods` | Billing type and period enums |
