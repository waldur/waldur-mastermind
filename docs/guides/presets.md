# Demo Presets

Demo presets provide pre-configured data sets for demonstrations, testing, and development.
Each preset contains users, organizations, projects, offerings, resources, and usage data.

## Available Presets

`waldur demo_presets list` is the authoritative list; `waldur demo_presets info <name>`
shows a preset's scenarios and entity counts.

| Preset | Description |
|--------|-------------|
| `minimal_quickstart` | Basic setup for quick demos and testing (default for the dev stack) |
| `government_cloud` | Multi-agency public-sector cloud with compliance-focused offerings |
| `public_sector_accounting` | Public-sector accounting and oversight: 2 public providers, 10 public bodies, service layers as category groups, GPU-hours per chip model, per-hour PaaS plans, 18 months of usage and invoices, offering users in screening states, a `governance` user with the global support role |
| `research_institution` | HPC and research computing environment |
| `hpc_ai_platform` | GPU clusters, AI/ML workloads, SLURM periodic policies |
| `ai_factory` | Multi-provider AI value chain with user demographics and 12 months of invoices |
| `efp` | EuroHPC federation: 10 hosting entities, maintenance announcements |
| `openstack_cloud` | OpenStack tenants, instances and volumes with backend scopes |
| `call_management` | The EuroHPC JU access calls, one per JU call template: peer review, conflict-of-interest detection, allocation |
| `reporting` | Small dataset for proposal reporting dashboards |
| `credit_management` | Organization and project credits with cost policies |
| `credit_realistic` | Production-shaped credit usage over six months |
| `credit_scenarios` | One project per credit-dashboard state |
| `discounts_and_affiliates` | Provider volume discounts and the affiliate programme |
| `usage_periods_demo` | Every component limit period (month, quarter, year, total) |
| `resource_renewal` | Resource renewal notification workflow |
| `e2e_testing` | Minimal data for Playwright end-to-end tests |
| `glauth` | LDAP (GLAuth) export testing |

Presets whose `_metadata.rebase_billing_history` is `true` (`credit_realistic`,
`public_sector_accounting`) have their invoices and usages moved on load so the newest month
is the current one; the others keep the dates they were generated with.

To regenerate `public_sector_accounting`:

```bash
python scripts/generate_public_sector_preset.py
```

## Management Commands

### List Available Presets

```bash
waldur demo_presets list
waldur demo_presets list --quiet  # Names only
```

### View Preset Details

```bash
waldur demo_presets info minimal_quickstart
```

### Load a Preset

```bash
# Load with confirmation prompt
waldur demo_presets load minimal_quickstart

# Skip confirmation
waldur demo_presets load minimal_quickstart --yes

# Preview without applying changes
waldur demo_presets load minimal_quickstart --dry-run

# Keep existing data (no cleanup)
waldur demo_presets load minimal_quickstart --no-cleanup

# Skip user import
waldur demo_presets load minimal_quickstart --skip-users
```

### Settings and Features

A load with cleanup (the default) returns every setting and every feature
flag to its default, then applies the preset's `constance_settings` and
`features`. Loading preset A and then preset B leaves the same settings as
loading B alone.

Settings the deployment owns survive the load unchanged, and a preset cannot
set them; the load reports each one it skips. A setting is owned when it is:

- locked, or re-applied on every deploy (`managed_by`)
- written by the deployment's configuration (`source=deployment`, such as
  values from `override_constance_settings`)
- a secret
- one Waldur changes while it runs, such as `TELEMETRY_DEPLOYMENT_ID`

A load with `--no-cleanup` leaves settings and features as they are and only
applies what the preset sets.

A load sends no notifications, publishes no messages to site agents and
queues no billing tasks. It fails if the cleanup cannot delete everything,
rather than importing over a half-cleaned database.

After loading, the command displays user credentials:

```text
============================================================
Demo User Credentials
============================================================
  staff: demo [staff]
  support: demo [support]
  owner: demo
  manager: demo
  member: demo
============================================================
```

### Export Current State

```bash
waldur demo_presets export my_preset --title "My Custom Setup"
```

## REST API

### List Presets

```http
GET /api/marketplace-demo-presets/list/
Authorization: Token <staff_token>
```

### Get Preset Details

```http
GET /api/marketplace-demo-presets/info/{name}/
Authorization: Token <staff_token>
```

### Load Preset

```http
POST /api/marketplace-demo-presets/load/{name}/
Authorization: Token <staff_token>
Content-Type: application/json

{
  "dry_run": false,
  "cleanup_first": true,
  "skip_users": false,
  "skip_roles": false
}
```

A load runs in the background. The request answers `202` with the load, or
`409` while another load is pending or running:

```json
{
  "uuid": "4f1c...",
  "preset": "minimal_quickstart",
  "state": "pending",
  "created": "2026-11-09T12:00:00Z",
  "started_at": null,
  "finished_at": null,
  "error_message": ""
}
```

A dry run is answered directly with `200`, its output and the preset's users.

A load with cleanup first (the default) drops every task queued for the
workers before it queues itself. Presets reuse fixed UUIDs, so a task queued
before the reset would otherwise act on the object of the same UUID that the
load creates. Tasks a worker has already taken still finish. The load's output
records how many tasks it dropped. If the task queue cannot be reached, the
request answers `503` (without `demo_reset`) and the load is recorded as
`erred`.

While a load is pending or running, every other `/api/` request answers `503`
with `{"demo_reset": true, "preset": "<name>"}` and a `Retry-After` header.
`/api/demo-preset-status/`, `/api/configuration/` and `/api-auth/` keep
answering, so a browser can follow the load and log in again. A load deletes
every user and token, the caller's included.

A load that is still active an hour after it started (the task's time limit)
was abandoned, typically by a deploy restarting the worker. It is reported as
`erred` and no longer blocks the API or new loads.

### Load Status

```http
GET /api/demo-preset-status/
```

Public, with no authentication, because the load deletes the caller's session.
It answers `404` unless the deployment enables demo presets.

```json
{
  "loading": false,
  "load": {"uuid": "4f1c...", "preset": "minimal_quickstart", "state": "done", "...": "..."},
  "users": [
    {"username": "owner", "password": "demo", "email": "owner@example.com", "is_staff": false, "is_support": false}
  ]
}
```

`users` lists the loaded preset's users with their passwords for quick login,
once the latest load is done. Users with a staff or support role are never
listed: their passwords would give anyone portal-wide access.

## Preset Contents

Each preset JSON file includes:

- `_metadata` - Title, description, version, scenarios
- `users` - User accounts with passwords
- `customers` - Organizations
- `projects` - Projects within organizations
- `offerings` - Service offerings with components
- `plans` - Pricing plans
- `resources` - Provisioned resources
- `component_usages` - Usage data per billing period
- `component_user_usages` - Per-user usage breakdown
- `user_roles` - Role assignments
- `constance_settings` - Site configuration
- `features` - Feature flags, as `{"key": "<section>.<feature>", "value": true}`

## Creating Custom Presets

1. Export current state or copy an existing preset
2. Place JSON file in `src/waldur_mastermind/marketplace/demo_presets/presets/`
3. Add `_metadata` section with title, description, version
4. Ensure all UUIDs are unique 32-character hex strings

### UUID Format

UUIDs must be exactly 32 hexadecimal characters (0-9, a-f):

```json
"uuid": "00000000000000000000000000000001"
```

### User Passwords

Include plaintext passwords in the `users` array:

```json
{
  "username": "demo_user",
  "password": "demo",
  "email": "demo@example.com"
}
```

## File Location

Presets are stored in:

```text
src/waldur_mastermind/marketplace/demo_presets/presets/
```
