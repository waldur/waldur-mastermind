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

Waldur supports a two-level credit system: **CustomerCredit** (organization-wide) and **ProjectCredit** (per-project allocation). Both inherit from `BaseCredit` (`src/waldur_mastermind/invoices/models.py`).

#### Credit Model

| Field | Type | Description |
|-------|------|-------------|
| `value` | Decimal | Remaining credit balance |
| `end_date` | Date (nullable) | Expiry date (must be 1st of month) |
| `expected_consumption` | Decimal | Target monthly spend |
| `minimal_consumption_logic` | `FIXED` / `LINEAR` | How expected consumption is managed |
| `grace_coefficient` | Decimal (0-100) | Percentage discount on minimal consumption |
| `apply_as_minimal_consumption` | Boolean | Whether to enforce minimal consumption |

**ProjectCredit** is a sub-allocation of the customer credit. A new or raised allocation is refused when the allocations together would exceed the customer credit value. Usage draws the customer credit down while the allocations stay as they are, so later on the allocations can exceed it; `ProjectCredit.spendable_value` caps what each can draw.

ProjectCredit also has three staff-only fields, described in [Paying earlier invoices](#paying-earlier-invoices): `pay_earlier_invoices`, `earlier_invoices_from` and `earlier_invoices_until`.

Every change to either balance is recorded in the [credit ledger](../credit-ledger.md), which is what makes the drawdown below observable after the fact: the minimal-consumption tail writes no invoice item, so the ledger is the only place it is visible.

#### Invoice Finalization Flow

During invoice finalization, credits are processed via `process_invoice_credits()`:

```mermaid
sequenceDiagram
    participant T as Invoice Task
    participant S as set_to_zero_overdue_credits
    participant MC as MonthlyCompensation
    participant DB as Database

    T->>S: Zero overdue credits
    S->>DB: Zero CustomerCredits where end_date < today
    S->>DB: Zero ProjectCredits where end_date < today

    T->>MC: process_invoice_credits(invoice)
    MC->>MC: clear_compensations() (rollback any previous)
    MC->>MC: calculate_current_compensations()
    MC->>MC: save() (write compensation items + update credits)
    MC->>MC: update_linear_expected_consumption()
    MC->>DB: Update expected_consumption for LINEAR credits

    T->>DB: pay_earlier_invoices_at_month_end(invoice)
```

#### Compensation Calculation

`MonthlyCompensation.calculate_current_compensations()` processes invoice items sorted by price (ascending):

1. For each item, check if the item's project has a **ProjectCredit**
2. If yes: deduct from the project credit first, then from the customer credit
3. If no: deduct directly from the customer credit
4. Create a negative `InvoiceItem` (compensation) for each deduction
5. After all items, enforce **minimal consumption** for both customer and project credits

```mermaid
sequenceDiagram
    participant MC as MonthlyCompensation
    participant PC as ProjectCredit
    participant CC as CustomerCredit
    participant INV as Invoice

    loop For each invoice item (sorted by price)
        alt Item's project has ProjectCredit
            MC->>PC: Deduct min(item.price, pc.value)
            MC->>CC: Deduct same amount from customer credit
        else No ProjectCredit
            MC->>CC: Deduct min(item.price, cc.value)
        end
        MC->>INV: Create negative InvoiceItem (compensation)
    end

    Note over MC: Enforce minimal consumption
    alt total_compensation < cc.minimal_consumption
        MC->>CC: Deduct shortfall (tail) from credit
    end
    loop For each ProjectCredit with minimal_consumption > 0
        alt project_compensation < pc.minimal_consumption
            MC->>PC: Deduct shortfall (tail) from credit
        end
    end
```

Both deductions against a balance — what usage consumed and what the floor took
— land in a single `value` write. The flow declares the breakdown so that the
ledger can record them as separate `compensation` and `minimal_draw` rows; see
[Credit ledger](../credit-ledger.md).

#### Minimal Consumption

Minimal consumption ensures a minimum credit spend per month, preventing credits from being hoarded.

**Formula**:

```text
If end_date is this month:
    minimal_consumption = expected_consumption

Otherwise:
    minimal_consumption = (100 - grace_coefficient) / 100 * expected_consumption
```

If `apply_as_minimal_consumption` is `False`, minimal consumption is 0 (disabled).

#### Minimal Consumption Logic: FIXED vs LINEAR

**FIXED** (default): `expected_consumption` is set manually and stays constant.

**LINEAR**: `expected_consumption` is recalculated each month to ensure the credit is consumed by `end_date`. The formula is:

```text
new_expected = max(0, old_expected - total_compensation) * (1 - time_left_factor)
             + remaining_value * time_left_factor

where:
    time_left_factor = min(1, days_in_current_month / days_until_end_date)
```

This creates a sliding target: as the end date approaches, `time_left_factor` increases toward 1.0, pushing `expected_consumption` toward the full remaining credit value. This guarantees the credit is consumed by expiry.

```mermaid
sequenceDiagram
    participant MC as MonthlyCompensation
    participant CC as CustomerCredit LINEAR
    participant PC as ProjectCredit LINEAR
    participant DB as Database

    MC->>MC: update_linear_expected_consumption()

    alt CustomerCredit has LINEAR logic + end_date
        MC->>CC: calculate_linear_expected_consumption(total_compensation)
        MC->>DB: Save new expected_consumption
    end

    MC->>DB: Query all ProjectCredits with LINEAR logic + end_date > today
    loop For each linear ProjectCredit
        MC->>PC: calculate_linear_expected_consumption(tail + project_compensation)
        MC->>DB: Save new expected_consumption
    end
```

#### Overdue Credit Zeroing

`set_to_zero_overdue_credits()` runs during invoice finalization and zeros both customer and project credits whose `end_date` has passed. Zeroing a project credit does **not** affect the customer credit balance.

When a grace period is used, the effective date for zeroing credits is always the 1st of the current month (not the actual finalization date). This ensures credits with `end_date` on the 1st are still applied to the previous month's invoice before being zeroed.

#### Paying earlier invoices

The compensation run only pays the month it closes. Cost that no credit covered stays uncovered, and a period-Total cost policy keeps counting it however much credit is added later. With `ProjectCredit.pay_earlier_invoices` on, what is left of the allocation also pays that cost (`src/waldur_mastermind/invoices/settlement.py`). Only staff may set it, because it changes what the organization was billed; owners editing the rest of the allocation get a validation error if they change it.

- **What it pays.** Closed (`created` or `paid`) invoices of the project, oldest month first. `earlier_invoices_from` and `earlier_invoices_until` optionally bound the months, inclusive, each named by its first day.
- **When it pays.**
    - Whenever the credit is saved through the API with the option on: when staff turn it on, and when the allocation is raised later. It pays from what is left after this month's usage so far: the live balance the cost policies use, the lower of the allocation's and the organization credit's.
    - At month end, in `process_invoice_credits`, after the closing month's own compensation, from what that left.
- **How it pays.**
    - It writes a credit compensation named `Credit compensation for YYYY-MM. <item>`, with `compensated_period` set to the first day of the month it pays.
    - `earlier_invoices_compensate_in` (staff-only) picks the invoice it goes on. With `current_month` (the default) that is the open (or closing) invoice, and earlier invoices stay as they were issued; the invoice it is written on can go below zero. With `incurred_month` it is the paid month's own invoice, which changes the total of an invoice already issued, and possibly paid.
    - Each invoice written on gets its own draw, ledgered as `historical_compensation` with that invoice as its reference and its month as `billing_period`.
- **What a month still needs** (`earlier_months`):
    - the project's items on that month's invoice, on offerings the credit covers, before tax;
    - less what paid them: compensations and volume discounts on the invoice, payments made later, and staff refunds of its items (`create_compensation`) on later invoices.
    - Items are taken by their `project`, not their resource's current project, so a moved resource does not bring its history along.
    - Compensations are netted by sign rather than by their `credit` link, which a deleted organization credit leaves empty.
- **Written once.**
    - `clear_compensations` leaves payments alone, and the month's remainder is worked out from what stands. A second save, a month-end run after a mid-month payment, or the two open invoices of a grace period therefore pay nothing twice.
    - Both writers lock the organization credit's row.
    - Turning the option off stops further payments. It does not reverse the ones made, which takes a staff credit adjustment.
- **Paying on request.** `POST /api/project-credits/<uuid>/pay_earlier_invoices/` (staff) pays now, turns on `pay_earlier_invoices` and stores `compensate_in` when given. With `dry_run` it returns the compensations it would write (month paid, invoice written on, resource, cost item, amount) and the project and organization credit before and after, and writes nothing. Both run the same plan (`settlement.plan`), so a preview and the payment that follows it agree.
- **Explaining it.** `GET /api/project-credits/<uuid>/earlier_invoices/` lists every closed month with its `cost`, `covered` and `uncovered`, and `in_period` for the months the credit pays. It also gives the total `uncovered` (what a Total policy counts), `uncovered_in_period`, and `payable_now`, the part the credit would pay now. Staff, support, owners and roles with `CUSTOMER.VIEW_ACCOUNTING` see it, and so do the project's own roles while the organization displays billing info in projects, as they then see the project's invoice items. `in_period` follows the credit's own bounds, which can be previewed with others.

A payment counts in the month it pays everywhere cost is shown by month: `invoices.utils.month_q` / `annotate_cost_month` are used by the cost policies, the project costs chart, costs for a period and the price estimates. Counted by its invoice's month instead, a payment would take the current month below zero. `consumption_last_month` leaves payments out, since a one-off payment is not a burn rate. `rebill_historical_usage` counts them as credit already drawn for the month.

#### Credit Events

| Event | Trigger |
|-------|---------|
| `reduction_of_customer_credit` | Compensation item created |
| `reduction_of_project_credit` | Compensation item created for project |
| `reduction_of_customer_credit_due_to_minimal_consumption` | Customer tail deducted |
| `reduction_of_project_credit_due_to_minimal_consumption` | Project tail deducted |
| `reduction_of_customer_expected_consumption` | LINEAR recalculation (customer) |
| `reduction_of_project_expected_consumption` | LINEAR recalculation (project) |
| `set_to_zero_overdue_credit` | Expired credit zeroed |
| `roll_back_customer_credit` | Compensation cleared |
| `roll_back_project_credit` | Compensation cleared |

#### Cost Policies and Compensation

`waldur_mastermind.policy`'s `ProjectEstimatedCostPolicy` and `CustomerEstimatedCostPolicy` (`src/waldur_mastermind/policy/models.py`) fire when their cost after credit is over `limit_cost`, inside `evaluate()` (`EstimatedCostPolicyMixin._credit_gates`). `is_triggered()` is `evaluate().triggered`; see [Decision record](#decision-record) for what else an evaluation reports.

```mermaid
graph TD
    A[New invoice item / credit change] --> B{cost this window<br>net of compensation<br>> limit_cost?}
    B -->|No| Z[Policy stays clear]
    B -->|Yes| F[Policy fires]
```

The cost sums every `InvoiceItem` for the project/customer across the policy's rolling window (1/3/12 months, or all months for Total) via `EstimatedCostPolicyMixin._evaluated_cost` -- cost items and compensation items together, unfiltered by type, since a compensation item is just a negative-priced row on the same invoice. A payment of an earlier invoice (see [Paying earlier invoices](#paying-earlier-invoices)) is counted in the month it pays, not in the month of the invoice it is written on, so it lowers a Total policy's cost and leaves a Month policy's alone. For a month whose invoice has already been finalized -- including one `rebill_historical_usage` has just corrected -- this sum is entirely real, persisted data. There's no simulation involved in reading it. The policy fires when that net cost is strictly greater than `limit_cost`.

On top of that sum, the evaluation additionally subtracts the credit the customer's *current, still-mutable* invoice will still draw (`_pending_compensation`). That comes from a `MonthlyCompensation(customer, restore_written=True)` simulation, re-run in memory on every evaluation and never saved (it refuses to `save()`). It has no bearing on any past month already reflected in the sum above. The simulation works in credit units, before tax, while a compensation item carries its invoice's tax like the cost it offsets, so the pending draw is taxed at the current invoice's rate before it comes off; otherwise a project its credit fully covers would show the tax as cost all month.

How much credit is left is **not** compared with `limit_cost`. Until #784 a second gate held a credit-aware policy back while the live balance stayed above the limit. With period Total, once earlier uncovered cost was above the limit, that gate alone decided, so `limit_cost` worked as a minimum remaining credit and a policy with a larger limit fired before one with a smaller limit.

The **live** credit balance is still computed, from the same simulation (`_live_credit_balance()`), for the resource-creation check and the ETA projection:

```text
live balance = stored value + restored − usage draw
```

- `restored` is what this month's applies already took (staff can apply compensations mid-month), read from the credit ledger: this billing period's `compensation`, `minimal_draw` and `rollback` rows, per organization credit and project allocation. `restore_written=True` adds it back to the simulation's in-memory credits first, so the month is simulated from its opening balance; re-simulating from a stored value an apply had already lowered would cap the draw there. The ledger is used, not the invoice items, because the minimal-consumption floor writes no item and is only partly taken when the credit left is smaller than it. A `historical_compensation` row is not added back: a payment of an earlier invoice is not one of the month's draws, and the stored value it lowered stands.
- `usage draw` is what the simulation records usage taking off the balance this month (`customer_usage_draw`, `get_project_usage_draw(project)`). Usage only: the minimal-consumption floor is forfeited at month end whatever the usage, so counting it would make a credit with an expected consumption read as spent on day 1.

A project with a `ProjectCredit` reads the lower of its live allocation and the organization's live balance, since the allocation can only be drawn while the organization credit funding it lasts (`ProjectCredit.spendable_value`); without one, the organization's live balance. `use_credit=False` policies compare gross cost instead.

Verified example, from `scripts/simulate_rebill_historical_usage.py`'s scenario B: a resource billed 500 node-hours (1,425), fully compensated at finalization time from a 3,000 project credit (1,575 left over). A correction raises the reported usage to 3,000 node-hours (8,550) -- the credit correction aborts (7,125 needed, only 1,575 available), so the compensation item stays at its old value. The cost for that project: 8,550 − 1,425 = 7,125, against a `limit_cost` of 3,000 -- the policy fires.

##### Decision record

`evaluate()` returns a `PolicyDecision` (`src/waldur_mastermind/policy/structures.py`) naming the check that decided the evaluation and the figures it used:

| Field | Meaning |
|-------|---------|
| `triggered` | Whether the policy fires |
| `gate` | Which check decided (see below) |
| `evaluated_cost` | The cost compared against `limit_cost`, net of `credit_deduction` |
| `credit_deduction` | The credit the current month will still draw that is not yet written as compensation items (`_pending_compensation`); compensation already written is part of the window's invoice total |
| `limit_cost`, `period`, `use_credit` | The policy settings it was judged on |
| `credit_value_compared` | Always empty since #784, which removed the credit-balance comparison; kept so the API field keeps its shape. Events leave it out, as they do every empty value |

| Gate | Fires | When |
|------|-------|------|
| `cost_below_limit` | No | The cost net of credit is at or below `limit_cost` |
| `cost_over_limit` | Yes | A credit applies and the cost net of credit is over `limit_cost` |
| `no_credit` | Yes | Cost over the limit and no credit applies: no credit exists, or an offering policy, which spans many customers |
| `credit_ignored` | Yes | Cost over the limit on a project policy with `use_credit=False` |

`held_by_project_credit`, `held_by_customer_credit` and `credit_exhausted` came from the removed credit-balance gate. They are no longer produced, but stay in `PolicyGateEnum` for the clients that still handle them, and in the context of older events.

The decision of the policy's own evaluation is kept on the instance as `policy.decision`. The immediate actions and resets that an evaluation runs -- from `utils._evaluate_policies` and from the policy serializer's `save()` -- run on that instance, and `_get_cost_policy_context` (`policy_actions.py`) adds the decision's fields to the action's event context: `triggered`, `gate`, `evaluated_cost`, `credit_deduction`, `limit_cost`, `period`, `use_credit`, `credit_value_compared`. A `request_*` event therefore shows the figures that fired it, and a `reset_*` event the figures that reset it, rather than balances re-read afterwards. The older `credit_balance` / `project_credit_balance` keys -- stored balances read at the time of the event -- stay for compatibility. The "A policy %s has fired / has not fired" log lines carry the same fields. Actions run without a preceding evaluation of the instance -- threshold actions from the invoice-item handler, resets when a policy is deleted -- carry only the older keys.

The resource-creation check (`_check_project_policies` / `_check_customer_policies` in `policy/handlers.py`) calls `would_trigger(cost, offering)`, which is `evaluate(cost, offering).triggered` (that hypothetical decision is not kept as `policy.decision`): the same comparison with the new resource's cost added, less what the live credit balance can still pay for it (`_added_cost_draw`). When the credit absorbs the whole added cost, the decision reads `cost_below_limit`. Offering policies are not evaluated this way -- their cost spans many customers and applies only to some -- so `evaluate()` with an added cost raises for them, and `_check_offering_policies` keeps its own check.

The kept decision is a snapshot: it stays on the instance until the next own evaluation replaces it, so code that runs an action later on the same instance should evaluate first.

##### Trigger status in the API

The cost-policy endpoints (`marketplace-project-estimated-cost-policies`, `-customer-`, `-offering-`) serve the current decision as `trigger_status`, so a client can say whether and why a policy fires without re-deriving the check:

```json
"trigger_status": {
  "gate": "cost_over_limit",
  "triggered": true,
  "credit_value_compared": null
}
```

`EstimatedCostPolicySerializer.get_trigger_status` calls the policy's own `evaluate()` with the response's shared per-customer `MonthlyCompensation` (the one `current_cost` and `eta_days` use), so listing many policies of one customer runs one simulation, and `triggered` always equals `is_triggered()`. The evaluation is kept per policy for the response (`_decision_for`), and `current_cost` reads its `evaluated_cost` instead of aggregating the invoices again; a `?field=` selection without `trigger_status` skips the evaluation, and `current_cost` then takes the cheaper path without the credit-balance queries. `triggered` is the evaluation now, while `has_fired` is the stored state of the last evaluation that ran actions; the two differ until the next one.

The API sends no sentence: `gate` is the `PolicyGateEnum` in the schema, and clients word the explanation themselves, in the user's language, from `gate`, `current_cost`, `limit_cost` and `credit_value_compared`.

Project policies are listed by project roles too, so the organization's figures follow the rule `ProjectCreditSerializer` applies: staff, support, organization owners and roles with `CUSTOMER.VIEW_ACCOUNTING` see them; for anyone else `customer_credit` is left out. The gate itself stays visible. Project credit stays visible to project roles, as on `ProjectCredit`.

##### Cost breakdown in the API

The same endpoints serve `cost_breakdown`, the parts `current_cost` is made of, from `get_cost_breakdown()`:

```text
current_cost = cost_before_credit − credit_drawn − credit_pending
```

| Field | Meaning |
|-------|---------|
| `cost_before_credit` | The invoiced cost within the policy's period before any credit, tax included |
| `credit_drawn` | Credit compensation already written on the period's invoices |
| `credit_pending` | Credit the current month will still draw that is not written yet (`_pending_compensation`); 0 for a policy that does not take credit into account, which still counts the compensation already written: that shows in `credit_drawn` |

The period's items are split by `credit__isnull`, the discriminator `_pending_compensation` uses, so the parts add up to `current_cost` exactly. It shares the response's `MonthlyCompensation` with `current_cost`, and a `?field=` selection without `cost_breakdown` skips it.

##### What started an evaluation, and who

Every event an evaluation's actions emit also records why the evaluation ran. `system_actor_event_context` (`policy/utils.py`) puts these fields into the event context, so they reach every event emitted within the evaluation, including the resource-save events its actions cause:

| Field | Meaning |
|-------|---------|
| `trigger` | What started it (`EvaluationTriggers` in `policy/structures.py`, see below) |
| `trigger_object_uuid` | The object behind the trigger: the invoice item, credit, component usage, policy or resource; absent for the periodic sweep |
| `initiated_by_uuid`, `initiated_by_username` | The person whose action started it; absent when nobody did, for example the sweep, a usage report from a site agent outside a request, or a background job that runs as a robot account (`ROBOT_USERNAMES`: `system_robot`, `openportal_robot`), which is never recorded as an initiator. Under impersonation the impersonator is the initiator, as for revisions |
| `initiated_as_username` | Under impersonation only: the account the initiator was acting as |

The event's own `user_*` fields stay the system robot whenever a person is replaced, so the robot is still recorded as the actor; the `initiated_by_*` fields keep the person.

A new trigger replaces the old one and its object together, so a context that still carries an earlier trigger's object never lends it to an unrelated evaluation. The initiator is the person acting now when there is one; a recorded initiator only carries over while the events are a robot's.

Celery copies the event context into every task published while an evaluation runs, its fields included, so the events of the work an evaluation sets off (an order it creates, say) still say what started it. Whether an evaluation is running *here* is tracked per thread (`utils.in_evaluation`), not read from the context: a task that merely carries an earlier evaluation's fields is not inside it.

| `trigger` | Path |
|-----------|------|
| `policy_saved` | The policy serializer's `save()`, when a create or update fires or resets the policy |
| `invoice_item_saved` | An invoice item saved, through the project, customer and offering cost-policy handlers |
| `credit_changed` | A `CustomerCredit` or `ProjectCredit` value change, or a change to the credit's offerings |
| `usage_reported` | A component usage saved, through the offering and customer usage-policy handlers and the SLURM periodic-usage policy dispatch. The SLURM evaluation does not run in `system_actor_event_context`, so its events keep the reporting user as the event user, who is also its `initiated_by_*` |
| `periodic_sweep` | `check_polices`, the beat task that re-evaluates every policy |
| `reconcile` | The sweep re-applying an already-fired policy's idempotent actions (`_reconcile_idempotent_actions`) |
| `resource_saved` | Not an evaluation: an already-fired policy's threshold actions, run when an observed resource is saved. The request user stays the event user, since they are the actor here. Inside an evaluation running in the same thread (an action saving the resource) the evaluation's trigger is kept; in a later task that only carries an earlier evaluation's fields, `resource_saved` replaces them |
| `policy_deleted` | Not an evaluation: a deleted project cost policy's actions being reset. The robot acts and the person who deleted it is the initiator, as on the `policy_saved` reset path |

A cost evaluation caused by usage billing is reported as `invoice_item_saved`, with the invoice item that billing upserted as its object: the usage report's own handlers dispatch only the usage policies.

The handler-driven evaluations run in Celery, away from the request. `_debounced_call` puts the trigger and the object's UUID into the event context while it publishes the task. `celeryconf.pass_event_context` already carries that context, request user included, to the worker, where `bind_event_context` restores it before the task runs. So `evaluate_policies_async` keeps its `(policy_class_path, filters)` signature, and old and new workers both run it during a rolling deploy. Debouncing keeps the first dispatch in its window: later triggers coalesced into it are evaluated, but the events name the first one. The fired / not fired log lines carry the same fields.

See `EstimatedCostPolicyMixin.evaluate` / `_credit_gates` / `_live_balance` and `ProjectEstimatedCostPolicy` / `CustomerEstimatedCostPolicy._live_credit_balance_with_source` in `src/waldur_mastermind/policy/models.py`, and `MonthlyCompensation._restore_written_draw` in `src/waldur_mastermind/invoices/compensations.py`, for the exact implementation.

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
| `src/waldur_mastermind/invoices/settlement.py` | `pay_earlier_invoices`, `earlier_months` | A project credit paying earlier, uncovered invoices |
| `src/waldur_mastermind/marketplace/enums.py` | `BillingTypes`, `LimitPeriods` | Billing type and period enums |
| `src/waldur_mastermind/policy/models.py` | `ProjectEstimatedCostPolicy`, `CustomerEstimatedCostPolicy` | Cost Policy gate logic, consumes `MonthlyCompensation` as a projection |
