# SLURM Periodic Usage Policy Configuration Guide

## Overview

The `SlurmPeriodicUsagePolicy` enables automatic management of SLURM resource allocations with:

- Periodic (quarterly) usage tracking
- Automatic QoS adjustments based on usage thresholds
- Carryover of unused allocations with decay
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
        fairshare_decay_half_life: 15
```

## Policy Parameters

### Core Parameters

- **`apply_to_all`**: `True` for all customers, `False` for specific groups
- **`organization_groups`**: Specific groups if not applying to all
- **`actions`**: Comma-separated list of actions to trigger

### SLURM-Specific Parameters

- **`limit_type`**: `"GrpTRESMins"`, `"MaxTRESMins"`, or `"GrpTRES"`
- **`tres_billing_enabled`**: Use TRES billing units vs raw values
- **`tres_billing_weights`**: Weight configuration for billing units
- **`fairshare_decay_half_life`**: Days for fairshare decay (default: 15)
- **`grace_ratio`**: Grace period ratio (0.2 = 20% overconsumption)
- **`carryover_enabled`**: Allow unused allocation carryover
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
    fairshare_decay_half_life=15,
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
    fairshare_decay_half_life=30,  # Slower decay
)
policy.organization_groups.add(consortium_members)
```

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

Two staff-only API actions allow testing policy evaluation directly from the frontend or API without waiting for automatic triggers.

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

- Review carryover settings and decay factor
- Check billing period alignment (quarterly boundaries)
- Verify component type matches between policy and usage data

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
