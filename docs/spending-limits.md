# Spending Limits

How a customer caps the use of a usage-based component per calendar period,
and how an integration reports usage per day so that day and week limits can be
enforced. The motivating case is metered inference billed in money (a
`token_cost` component priced at 1 per unit), but nothing here is specific to
it.

## Overview

- A **usage-based** component is billed on what was used. Its provider may let
  customers set **spending limits** on it: at most one per period, for any of
  day, week, month, quarter, year and total.
- Limits are **never billed** and are not stored in `Resource.limits`, which
  holds the quantities of limit-based components.
- Every limit compares a different span of the same usage. With limits of 20 a
  day, 100 a week and 300 a month:

```text
Mon 19   18
Tue 20   20   daily limit reached: paused until Wed 00:00 UTC
Wed 21   15

daily    today          15 of 20
weekly   Mon 19-Wed 21  53 of 100
monthly  Oct 1-Oct 21  142 of 300

October invoice = sum of October's days x price. The limits are never billed.
```

- Periods are **calendar periods in UTC**. A week starts on Monday, a quarter on
  1 January, April, July or October.
- **Total** is the exception: all the usage since the resource started, which
  never resets. It suits a one-off budget, such as a grant of 500 USD for a
  pilot, which a yearly limit would hand out again every January.
- A reached limit applies the offering's `action_on_usage_limit` (`pause` or
  `downscale`). The restriction lifts when every limit is under again, and at
  the latest within five minutes of the reset of the limits that hold it.
- An offering without `action_on_usage_limit` accepts limits and reports their
  status, but nothing restricts the resource when one is reached.

## Provider: offering components

`OfferingComponent.spending_limit_periods` names the periods offered, each with
an optional `default` (what the order form starts from) and `maximum`:

```json
{
  "type": "token_cost",
  "billing_type": "usage",
  "spending_limit_periods": {
    "day": {"default": 20, "maximum": 100},
    "week": {},
    "month": {"default": 300, "maximum": 1000}
  }
}
```

It is accepted on usage-based components only, and not on a component whose
limit a resource option already shows (`display_limit_config`). A shorter
period's default may not exceed a longer one's. Amounts take at most two decimal
places, matching `ComponentUsage.usage`.

Day and week limits need the integration to **report usage per day** (below).
An integration that reports month totals can offer month, quarter, year and
total.

`limit_amount` on a usage-based component is unrelated and not enforced: a
report above it is accepted and billed, with a log warning.

## Customer: setting limits

At order time, `POST /api/marketplace-orders/` accepts `spending_limits`:

```json
{"spending_limits": {"token_cost": {"day": 20, "month": 300}}}
```

Each value must be one of the offered periods, non-negative, within the
period's maximum, and a shorter period's limit may not exceed a longer one's.
The values are stored on the new resource as `Resource.spending_limits`.

Afterwards, `POST /api/marketplace-resources/{uuid}/set_spending_limits/`
replaces them, with no order and no provider approval. A missing or `null`
period clears that limit, and `{}` clears all. It needs
`RESOURCE.SET_LIMITS` (`PermissionEnum.UPDATE_RESOURCE_LIMITS`) on the
project or organization, logs
`marketplace_resource_spending_limits_updated`, and re-evaluates the
restriction before it responds.

## Reporting usage per day

`POST /api/marketplace-component-usages/set_usage/` (and the signed
`/api/marketplace-public-api/set_usage/`) accept `daily_usages` next to
`usages`:

```json
{
  "resource": "resource-uuid",
  "usages": [],
  "daily_usages": [
    {"type": "token_cost", "date": "2026-10-19", "amount": "18.00"},
    {"type": "token_cost", "date": "2026-10-20", "amount": "20.00"}
  ]
}
```

- `date` is a UTC date. Reporting a date again **replaces** its amount, so an
  agent can resend the whole month so far on every run.
- Only usage-based components take daily amounts. A date in the future, or the
  same component and date twice in one request, is rejected, and so is a
  request of more than 1000 daily items (`MAX_DAILY_USAGE_ITEMS`): every day of
  two months for sixteen components.
- A date in a past month needs staff, or the service provider while that
  month's invoice can still change — the same rule as a dated monthly report.
- The amounts are stored in `ComponentDailyUsage`. The month's `ComponentUsage`
  rows are then recomputed as the sum of its days, per plan: a day belongs to
  the plan period in force at its end (or now, for today). Invoicing reads the
  month rows as for a monthly report, so the invoice and the days agree.
- Once a month has daily amounts, a monthly total for that component and month
  is rejected with 400; the next daily report would overwrite it. A month total
  sent before the first daily report is replaced by the days, so an
  integration that switches to daily reporting mid-month must report every day
  of the month so far.

Integrations that only report monthly totals are unaffected.

## Enforcement

`utils.evaluate_usage_limit_restriction` runs on every usage report that can
change the outcome, on `set_spending_limits`, every hour for restricted
resources, and every five minutes for resources whose
`usage_limit_resumes_at` has passed (`lift_reset_spending_limit_restrictions`).

- Day and week sum `ComponentDailyUsage`; a week spans month boundaries. Month,
  quarter and year sum the monthly `ComponentUsage` rows, and total sums every
  one of them since the resource started. Months the plan billed the component
  by limit do not count for any of them.
- A limit is reached when usage is equal to it or above it, so a limit of 0
  keeps the resource restricted for as long as it is set.
- `Resource.usage_limit_resumes_at` is when every reached spending limit has
  reset. It is empty while a limit-based component's own limit is also
  reached, because that one lifts on its own period's rollover, and while a
  reached total holds the resource: a total never resets, so the restriction
  lifts only when the limit is raised or cleared (`set_spending_limits`
  re-evaluates at once).
- A restriction applied by hand before a limit is reached is never taken over
  or lifted here (`Resource.usage_limit_restriction` records which restriction
  this mechanism applied). A pause set by the project grace period
  (`paused_by_grace_period`) or held by a registered keeper
  (`utils.PAUSE_KEEPERS`, such as a firing cost policy) is marked as this
  mechanism's too once a limit is reached, so releasing the other reason leaves
  the resource paused while it is over the limit. Either way the pause is kept
  past the reset while the grace period or a keeper still wants it. Downscaling
  has no such keepers.
- Usage is checked when it is reported, so spending can go over a limit between
  two reports. The order form says so.
- A limit stays in force if the provider later stops offering its period. It is
  not enforced while the resource's plan bills the component by limit.

## Resource API

The resource returns, besides `spending_limits` and `usage_limit_resumes_at`,
one `spending_limit_status` entry per component and period:

```json
{
  "component_type": "token_cost",
  "period": "day",
  "limit": 20.0,
  "used": 20.0,
  "resets_at": "2026-10-22T00:00:00Z",
  "reached": true,
  "last_reported_at": "2026-10-21T11:58:03Z"
}
```

`resets_at` is `null` for a total, which never resets.

Status is computed only for resources that have spending limits.

## API keys

A resource's API keys take limits of the same shape, per component and period,
each capped by the lowest of the resource's limits for that period and the
longer ones; a reached key limit pauses that key alone, and a key paused for
its limit stays paused while the resource is restricted for its own. The
offering decides which periods keys are offered: `month` only, unless it
declares with `plugin_options.api_key_daily_usage` that the agent reports each
key's usage per day, and none where `plugin_options.api_key_limits` is off.
A total is offered wherever the component offers it, as it needs no daily
usage: it adds each closed month's figure, which a key keeps in its usage
history, to the current month's. A month's figure never drops, and the agent
may still raise a closed month's after the month ends, so the usage it sends
just after midnight counts. See [Resource API keys](resource-api-keys.md#limits).

## Code map

| Concern | Where |
|---|---|
| Periods, validation, usage per period, status | `marketplace/spending_limits.py` |
| Daily rows and the month rows derived from them | `marketplace/daily_usage.py` |
| Restriction and resume time | `marketplace/utils.py` (`evaluate_usage_limit_restriction`) |
| Reset task | `marketplace/tasks.py` (`lift_reset_spending_limit_restrictions`) |
| Report handlers | `marketplace/handlers.py` (`evaluate_usage_limit_on_*_report`) |
