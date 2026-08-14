# SLURM Periodic Usage Policy Configuration Guide

## Overview

The `SlurmPeriodicUsagePolicy` enables automatic management of SLURM resource allocations with:

- Periodic usage tracking (monthly, quarterly, annual, or total)
- Automatic QoS adjustments based on usage thresholds
- Automatic period boundary reset (daily Celery beat task clears stale pauses/downscales when a new period starts)
- Carryover of unused allocations with configurable cap
- Grace periods for temporary overconsumption
- Integration with site agent for SLURM account management

## Available Actions

### Core Actions (Inherited from OfferingPolicy)

1. **`notify_organization_owners`** - Send email notifications to organization owners
2. **`notify_external_user`** - Send notifications to external email addresses
3. **`block_creation_of_new_resources`** - Block creation of new SLURM resources

### SLURM-Specific Actions

1. **`request_slurm_resource_downscaling`** - Apply slowdown QoS (sets `resource.downscaled = True`)
2. **`request_slurm_resource_pausing`** - Apply blocked QoS (sets `resource.paused = True`)

## How It Works

### Threshold Triggers

The policy checks usage percentages and triggers actions at different thresholds:

- **80%**: Notification threshold (hardcoded)
- **100%**: Normal threshold - triggers `request_slurm_resource_downscaling`
- **120%** (with 20% grace): Grace limit - triggers `request_slurm_resource_pausing`

### Site Agent Integration

When actions are triggered:

1. `request_slurm_resource_downscaling` → Site agent applies `qos_downscaled` (e.g., "limited")
2. `request_slurm_resource_pausing` → Site agent applies `qos_paused` (e.g., "paused")
3. Normal state → Site agent applies `qos_default` (e.g., "normal")

### Evaluation Lifecycle and Concurrency

Each new or updated `ComponentUsage` queues an asynchronous evaluation of the
affected resource against every applicable policy. Staff-triggered
`evaluate` / `force-period-reset` API calls and the daily period-boundary task
queue evaluations too. As a result, **several evaluations of the same resource
can run on different Celery workers at the same time** — for example, one fired
by a high-usage report and another fired moments later by a corrected,
lower-usage report.

A single evaluation of one resource performs the following steps:

1. Re-reads the resource's live usage for the current period (no value is cached
   from when the task was queued).
2. Decides the target `paused` / `downscaled` state from that usage and the
   policy thresholds.
3. Applies the change and, if the state actually changed, publishes a STOMP
   message to the site agent.

To keep concurrent evaluations from interfering, evaluations of the **same**
resource are serialized: each one locks the resource row and recomputes usage
under the lock before deciding. This guarantees that the **most recently
committed usage wins** — a slow evaluation that started from an old, high usage
reading cannot overwrite a newer evaluation that has already restored the
resource based on lower usage. Evaluations of *different* resources still run
fully in parallel.

!!! note
    Because the decision is always recomputed from live usage under the lock,
    re-running an evaluation is safe and idempotent. If usage has not changed,
    the re-run is a no-op and emits no STOMP message.

## Configuration Examples

### 1. Basic Notification Policy

Send notifications when usage reaches 80%:

```python
from waldur_mastermind.policy import models

policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=slurm_offering,
    actions="notify_organization_owners",
    apply_to_all=True,
    grace_ratio=0.2,
    carryover_enabled=True,
)
```

### 2. Progressive QoS Management

Apply slowdown at 100% usage with notifications:

```python
policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=slurm_offering,
    actions="notify_organization_owners,request_slurm_resource_downscaling",
    apply_to_all=True,
    grace_ratio=0.2,
    carryover_enabled=True,
)
```

### 3. Full Enforcement Policy

Complete enforcement with notifications, slowdown, and blocking:

```python
# Policy for 100% threshold
threshold_policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=slurm_offering,
    actions="notify_organization_owners,request_slurm_resource_downscaling,block_creation_of_new_resources",
    apply_to_all=True,
    grace_ratio=0.2,
    carryover_enabled=True,
)

# Additional policy for grace limit (would need separate instance)
grace_policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=slurm_offering,
    actions="notify_external_user,request_slurm_resource_pausing",
    apply_to_all=True,
    grace_ratio=0.2,
    options={"notify_external_user": "hpc-admin@example.com"},
)
```

### 4. Organization-Specific Policy

Apply policy only to specific organization groups:

```python
research_group = OrganizationGroup.objects.get(name="Research Universities")

policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=slurm_offering,
    actions="request_slurm_resource_downscaling",
    apply_to_all=False,  # Not universal
    grace_ratio=0.3,  # 30% grace for research
    carryover_enabled=True,
)
policy.organization_groups.add(research_group)
```

## Site Agent Configuration

### Prerequisites

Periodic settings are delivered to the cluster over STOMP only — there is no
polling fallback. Mastermind publishes them to queues that the site agent
registers on startup, so all of the following must hold or the policy silently
enforces nothing:

- **A site agent runs in `event_process` mode for this offering.** This is the
  only mode that registers event subscriptions. The polling modes
  (`order_process`, `membership_sync`, `report`) never do, so a deployment
  running only those cannot use periodic policies. In the Helm chart this is
  `agents.eventProcess.enabled`.
- **`stomp_enabled: true` on the offering** in the agent configuration. It
  defaults to `false`, and an offering without it is skipped entirely.
- **`backend_settings.periodic_limits.enabled: true`** on the offering, as shown
  below.
- **Site agent 0.8.0 or newer.** Earlier versions ignore `periodic_limits`.

Subscriptions are computed once, at agent startup: after changing any of the
above, restart the **`event_process`** agent specifically.

To confirm registration succeeded, look for these lines in the `event_process`
agent log:

```text
Periodic limits enabled for offering <name>, subscribing to periodic limits updates
Creating event subscription queue for <uuid>, object type ...RESOURCE_PERIODIC_LIMITS
```

Staff can verify the same from the API:

```text
GET /api/event-subscription-queues/?offering_uuid=<uuid>&object_type=resource_periodic_limits
```

An empty result is what drives the "No site agent has registered a queue for
periodic limits updates" warning on the policy.

### Configuration

Configure the site agent to handle QoS changes:

```yaml
# waldur-site-agent-config.yaml
offerings:
  - name: "SLURM HPC Cluster"
    backend_type: "slurm"
    backend_settings:
      # QoS mappings
      qos_downscaled: "slowdown"   # Applied at 100% usage
      qos_paused: "blocked"        # Applied at grace limit
      qos_default: "normal"        # Applied when below thresholds

      # Periodic limits configuration
      periodic_limits:
        enabled: true
        limit_type: "GrpTRESMins"
        tres_billing_enabled: true
        tres_billing_weights:
          CPU: 0.015625
          Mem: 0.001953125G
          "GRES/gpu": 0.25
```

## Policy Parameters

### Core Parameters

- **`apply_to_all`**: `True` for all customers, `False` for specific groups
- **`organization_groups`**: Specific groups if not applying to all
- **`actions`**: Comma-separated list of actions to trigger

### SLURM-Specific Parameters

- **`period`**: Billing period length — `MONTH_1` (monthly, default), `MONTH_3` (quarterly), `MONTH_12` (annual), or `TOTAL` (cumulative, never resets). Controls how `_get_current_period()` computes the billing window for usage calculations and carryover. Note: if the offering's components have a `limit_period` set, it takes precedence over this field.
- **`limit_type`**: `"GrpTRESMins"`, `"MaxTRESMins"`, or `"GrpTRES"`
- **`tres_billing_enabled`**: Use TRES billing units vs raw values
- **`tres_billing_weights`**: Weight configuration for billing units
- **`grace_ratio`**: Grace period ratio (0.2 = 20% overconsumption). The pause threshold is `(1 + grace_ratio) * 100`%. For example, `grace_ratio=0.2` means resources are paused at 120% usage.
- **`carryover_enabled`**: Allow unused allocation carryover between periods
- **`carryover_factor`**: Maximum percentage of base allocation that can carry over from unused previous period (integer, 0-100, default: 50). For example, `carryover_factor=50` means up to 50% of the base limit can be carried over. Unused allocation from the previous period is `max(0, base - prev_usage)`, capped at `(carryover_factor / 100) * base`.
- **`raw_usage_reset`**: Reset SLURM raw usage at period transitions
- **`qos_strategy`**: `"threshold"` or `"progressive"`

## Usage Scenarios

### Scenario 1: Academic Institution with Quarterly Allocations

```python
# 1000 node-hours per quarter with 20% grace
policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=academic_slurm,
    actions="notify_organization_owners,request_slurm_resource_downscaling",
    apply_to_all=True,
    limit_type="GrpTRESMins",
    grace_ratio=0.2,
    carryover_enabled=True,
)

# Add component limit
models.OfferingComponentLimit.objects.create(
    policy=policy,
    component=node_hours_component,
    limit=1000,
)
```

### Scenario 2: Commercial Cloud with Strict Limits

```python
# No grace period, immediate blocking
policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=commercial_slurm,
    actions="request_slurm_resource_pausing,block_creation_of_new_resources",
    apply_to_all=True,
    grace_ratio=0.0,  # No grace period
    carryover_enabled=False,  # No carryover
)
```

### Scenario 3: Research Consortium with Flexible Limits

```python
# Generous grace period with carryover
policy = models.SlurmPeriodicUsagePolicy.objects.create(
    offering=consortium_slurm,
    actions="notify_organization_owners",
    apply_to_all=False,
    grace_ratio=0.5,  # 50% grace period
    carryover_enabled=True,
)
policy.organization_groups.add(consortium_members)
```

## Worked Examples

The configuration snippets above define *policies*; the timelines below show how
a policy behaves as real usage evolves. They assume a monthly policy with a
1000 node-hour limit, `grace_ratio=0.2` (pause at 120%), and
`actions="notify_organization_owners,request_slurm_resource_downscaling,request_slurm_resource_pausing"`.

### Example A: A project that overshoots mid-month, then is topped up

A research project burns through its monthly allocation early, gets throttled,
and is restored after the owner buys more node-hours.

| Day | Usage | % of limit | Evaluation result | Resource state | Site agent QoS |
|-----|-------|-----------|-------------------|----------------|----------------|
| 1   | 200   | 20%       | below thresholds  | normal         | `normal`       |
| 8   | 820   | 82%       | notify owners     | normal         | `normal`       |
| 14  | 1010  | 101%      | downscale         | downscaled     | `slowdown`     |
| 18  | 1250  | 125%      | downscale + pause | downscaled + paused | `blocked` |
| 20  | limit raised to 2000 → 1250/2000 = 63% | 63% | clear pause + downscale | normal | `normal` |

Each row is the state after that day's usage report is evaluated. Note that
raising the limit on day 20 drops the percentage below all thresholds, so the
next evaluation restores the resource — no manual QoS change is needed.

### Example B: A corrected usage report (concurrency-safe restore)

Accounting first reports a usage spike, then immediately corrects it down. Two
evaluations race, but the resource still ends in the correct, restored state.

| Time      | Event                                  | Usage seen | Evaluation outcome              |
|-----------|----------------------------------------|-----------|---------------------------------|
| 10:00:00  | Usage reported as 1500 (150%)          | 150%      | evaluation A queued → pause + downscale |
| 10:00:03  | Correction: usage overwritten to 0     | 0%        | evaluation B queued → clear all |
| 10:00:04  | Evaluation B commits first             | 0%        | resource restored to normal     |
| 10:00:07  | Evaluation A finally runs              | 0% (re-read under lock) | no-op — usage is now 0%, nothing to pause |

Because evaluation A recomputes usage under the row lock, it sees the corrected
0% rather than the stale 150% it was triggered with, and leaves the restored
state untouched. The final state is `normal` / `qos_default`. See
[Evaluation Lifecycle and Concurrency](#evaluation-lifecycle-and-concurrency).

### Example C: Carryover across a period boundary

A monthly policy with `carryover_enabled=True` and `carryover_factor=50`
(limit 1000) lets an under-using month lift the next month's effective ceiling.

| Period   | Base limit | Prev-period usage | Carryover | Effective limit | 700 node-hours used → % |
|----------|-----------|-------------------|-----------|-----------------|--------------------------|
| January  | 1000      | —                 | 0         | 1000            | 70% → normal             |
| February | 1000      | 300 (Jan)         | min(700, 500) = 500 | 1500  | 700/1500 = 47% → normal  |
| March    | 1000      | 1400 (Feb, over)  | 0         | 1000            | 700/1000 = 70% → normal  |

January's unused 700 node-hours carries into February capped at 50% of the base
(500), raising February's ceiling to 1500. February overshoots, so March starts
fresh at the base limit with no carryover. See
[Debug Carryover Calculations](#debug-carryover-calculations) for the formula.

## API Usage

### Create Policy via API

```bash
curl -X POST https://waldur.example.com/api/marketplace-slurm-periodic-usage-policies/ \
  -H "Authorization: Token YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "OFFERING_UUID",
    "actions": "notify_organization_owners,request_slurm_resource_downscaling",
    "apply_to_all": true,
    "grace_ratio": 0.2,
    "carryover_enabled": true,
    "component_limits_set": [
      {
        "type": "node_hours",
        "limit": 1000
      }
    ]
  }'
```

### Check Policy Status

```bash
curl https://waldur.example.com/api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/ \
  -H "Authorization: Token YOUR_TOKEN"
```

## Evaluation and Testing

### Staff-Only API Actions

Three staff-only API actions allow testing and managing policy evaluation directly from the frontend or API without waiting for automatic triggers.

#### Dry Run

Calculate usage percentages and show what actions would be triggered without applying any changes.

```bash
curl -X POST https://waldur.example.com/api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/dry-run/ \
  -H "Authorization: Token STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Optionally scope to a single resource:

```bash
curl -X POST .../POLICY_UUID/dry-run/ \
  -H "Authorization: Token STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resource_uuid": "RESOURCE_UUID"}'
```

Response includes per-resource: `usage_percentage`, current `paused`/`downscaled` state, and `would_trigger` actions.

#### Evaluate (Synchronous)

Run the full evaluation: calculate usage, apply actions (pause/downscale/notify), and create evaluation log entries.

```bash
curl -X POST https://waldur.example.com/api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/evaluate/ \
  -H "Authorization: Token STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Response includes per-resource: `usage_percentage`, `actions_taken`, `previous_state`, and `new_state`.

#### Force Period Reset (Staff-Only)

Force-trigger a period boundary reset for a specific policy. This is useful after a Celery beat outage, or to immediately unblock resources that are still paused/downscaled from a previous period.

The action finds all active resources under the policy's offering that are currently paused or downscaled and have usage below 100% in the current period, then re-evaluates them synchronously — which removes the stale pause/downscale flags and sends STOMP messages to the site agent.

```bash
# Reset all stale paused/downscaled resources for a policy
curl -X POST https://waldur.example.com/api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/force-period-reset/ \
  -H "Authorization: Token STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Optionally scope to a single resource:

```bash
curl -X POST .../POLICY_UUID/force-period-reset/ \
  -H "Authorization: Token STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resource_uuid": "RESOURCE_UUID"}'
```

Response includes per-resource: `usage_percentage`, `actions_taken`, `previous_state`, and `new_state`.

#### Frontend

Staff users see an **Evaluate** button on the SLURM policy configuration panel. This opens a dialog with:

- **Dry run** — read-only preview of what would happen
- **Evaluate now** — runs the full evaluation synchronously and shows results

### Management Commands

Three management commands are available for CLI-based testing and monitoring:

#### evaluate_slurm_policy

```bash
# Dry run: show what would happen without applying changes
waldur evaluate_slurm_policy --policy <UUID> --dry-run

# Dry run for a single resource
waldur evaluate_slurm_policy --policy <UUID> --resource <UUID> --dry-run

# Run synchronously (blocking, results printed immediately)
waldur evaluate_slurm_policy --policy <UUID> --sync

# Queue async Celery tasks (check worker logs for results)
waldur evaluate_slurm_policy --policy <UUID>
```

#### slurm_policy_status

```bash
# Show all policies with resource states, evaluation logs, command history
waldur slurm_policy_status

# Single policy with more history
waldur slurm_policy_status --policy <UUID> --logs 50 --commands 20

# Filter to a specific resource
waldur slurm_policy_status --policy <UUID> --resource <UUID>
```

#### cleanup_slurm_logs

```bash
# Manually trigger evaluation log cleanup (uses constance retention setting)
waldur cleanup_slurm_logs
```

## Monitoring and Observability

### Evaluation Log

Every policy evaluation creates a `SlurmPolicyEvaluationLog` record with:

- `usage_percentage` — resource usage at the time of evaluation
- `grace_limit_percentage` — the grace threshold that was applied
- `actions_taken` — list of actions triggered (e.g. `["downscale", "notify"]`)
- `previous_state` / `new_state` — `paused` and `downscaled` flags before and after
- `stomp_message_sent` — whether a STOMP message was published to the site agent
- `site_agent_confirmed` — whether the site agent reported success (null = pending)
- `site_agent_response` — full response from the site agent

### Command History

When STOMP messages are sent to the site agent, each generated SLURM command is recorded in `SlurmCommandHistory`:

- `command_type` — e.g. `fairshare`, `limits`, `qos`, `reset_usage`
- `shell_command` — the actual `sacctmgr` command
- `execution_mode` — `production` or `emulator`
- `success` / `error_message` — filled in by site agent report-back

### API Endpoints

```bash
# List evaluation logs for a policy (filterable by resource_uuid, billing_period)
GET /api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/evaluation-logs/

# List command history for a policy (filterable by resource_uuid)
GET /api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/command-history/

# Site agent reports command execution result
POST /api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/report-command-result/
```

### Frontend Execution Log

The SLURM policy panel includes:

- **Status summary** — inline card showing last evaluation timestamp, count of paused/downscaled resources, and site agent confirmation status
- **Execution log** dialog with two tabs:
  - **Evaluation History** — table with timestamps, resource names, usage percentages (colour-coded), action badges, and state transitions
  - **Command History** — table with command types, shell commands, execution mode, and success/failure status

### Structured Events

Policy evaluations emit a `SLURM_POLICY_EVALUATION` event type, visible in the Waldur events system.

### Automatic Period Boundary Reset

A daily Celery beat task (`reset-slurm-policy-periods`, runs at 01:00) ensures that resources paused or downscaled in a previous period are automatically unblocked when the new period starts with zero usage.

For each `SlurmPeriodicUsagePolicy` (except those with `period=TOTAL`), the task:

1. Finds active resources that are still `paused=True` or `downscaled=True`
2. Checks if their usage in the **current** period is below 100%
3. If so, queues `evaluate_resource_against_policy` which clears the stale flags and sends STOMP messages to the site agent

This is idempotent — safe to re-run and catches up automatically after Celery beat outages. For immediate manual intervention, use the staff-only `force-period-reset` API action.

### Log Retention

Evaluation logs are automatically cleaned up by a daily Celery beat task (`cleanup-slurm-evaluation-logs`, runs at 03:00). The retention period is configurable via:

- **Constance setting**: `SLURM_POLICY_EVALUATION_LOG_RETENTION_DAYS` (default: 90 days)
- **HomePort admin**: Administration > Marketplace > SLURM policy

### Check Resource Usage (Django Shell)

```python
policy = SlurmPeriodicUsagePolicy.objects.get(offering=offering)
resource = Resource.objects.get(uuid="RESOURCE_UUID")

usage_percentage = policy.get_resource_usage_percentage(resource)
print(f"Current usage: {usage_percentage:.1f}%")
```

### Debug Carryover Calculations

Carryover allows unused allocation from the previous period to increase the current period's effective limit. The formula is:

1. `unused = max(0, base_limit - previous_period_usage)`
2. `cap = (carryover_factor / 100) * base_limit`
3. `carryover = min(unused, cap)`
4. `effective_limit = base_limit + carryover`

Example: base limit 1000, previous usage 400, carryover_factor 50 (i.e. 50%):

- `unused = max(0, 1000 - 400) = 600`
- `cap = (50 / 100) * 1000 = 500`
- `carryover = min(600, 500) = 500`
- `effective_limit = 1000 + 500 = 1500`

If the previous period was fully used (e.g., usage 1200), carryover is 0.

```python
settings = policy.calculate_slurm_settings(resource)
print(f"Carryover details: {settings['carryover_details']}")
print(f"Total allocation: {settings['carryover_details']['total_allocation']} node-hours")
```

## Site Agent Feedback Loop

After the site agent applies SLURM commands, it reports results back to Waldur:

1. Site agent receives STOMP message with `action: apply_periodic_settings`
2. Site agent executes `sacctmgr` commands via the backend
3. Site agent POSTs the result to `/api/marketplace-slurm-periodic-usage-policies/{policy_uuid}/report-command-result/`
4. Waldur updates `SlurmCommandHistory.success` and `SlurmPolicyEvaluationLog.site_agent_confirmed`

The STOMP message payload includes `policy_uuid` so the site agent knows which policy endpoint to report to.

## Best Practices

1. **Start with Notifications**: Begin with notification-only policies to understand usage patterns
2. **Use Dry Run First**: Run `waldur evaluate_slurm_policy --dry-run` or the frontend Dry Run button before enabling enforcement
3. **Test in Staging**: Validate policies in a test environment first
4. **Monitor Grace Periods**: Ensure grace ratios align with user needs
5. **Review Evaluation Logs**: Check the execution log regularly for unexpected actions
6. **Regular Review**: Review carryover and decay settings quarterly
7. **Clear Communication**: Inform users about thresholds and consequences

## Troubleshooting Common Issues

### Policy Not Triggering

- Check that `apply_to_all=True` or resource's customer is in `organization_groups`
- Verify component usage data exists for the current period
- Ensure resource is not in TERMINATED state
- Run `waldur evaluate_slurm_policy --policy <UUID> --dry-run` to see current usage percentages

### QoS Not Changing

- Verify site agent configuration has correct QoS names
- Check site agent logs for SLURM command execution
- Ensure resource backend_id matches SLURM account name
- Check the command history endpoint or `waldur slurm_policy_status` for sent commands and site agent responses

### Incorrect Usage Calculations

- Review carryover settings and carryover factor
- Check billing period alignment — the `period` field controls boundaries: `MONTH_1` (monthly, default), `MONTH_3` (quarterly), `MONTH_12` (annual), `TOTAL` (cumulative). Note that offering component `limit_period` overrides this field if set.
- Verify component type matches between policy and usage data

### Resources Still Paused After New Period Starts

- The `reset-slurm-policy-periods` task runs at 01:00 daily and should clear stale pauses. Check Celery worker logs for errors.
- Use the staff `force-period-reset` endpoint to manually trigger a reset: `POST /api/marketplace-slurm-periodic-usage-policies/POLICY_UUID/force-period-reset/`
- Verify that the policy's `period` is not set to `TOTAL` (total-period policies never auto-reset)

### No Evaluation Logs Appearing

- Confirm the evaluation was triggered (check Celery worker logs)
- Verify the policy has resources in the offering
- Use the staff Evaluate button or `waldur evaluate_slurm_policy --sync` to run synchronously and see immediate results

### Site Agent Not Reporting Back

- Check that `policy_uuid` is present in the STOMP message payload
- Verify the site agent has network access to the Waldur API
- Check site agent logs for HTTP errors when POSTing to `report-command-result`

## Migration from Manual Management

For organisations transitioning from manual SLURM management:

1. **Audit Current Allocations**: Document existing quotas and QoS settings
2. **Create Initial Policies**: Start with generous grace periods
3. **Enable Notifications First**: Monitor before enforcing — use the execution log to verify calculations
4. **Dry Run Testing**: Use the staff dry-run feature to validate policy behaviour before enabling enforcement actions
5. **Gradual Enforcement**: Phase in QoS changes over 2-3 quarters
6. **User Training**: Educate users about automatic management
