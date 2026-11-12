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

Where the deployment sets a staff password (see
[Deployment Presets](#deployment-presets)), staff and support users show
`(the deployment's staff password)` in place of the published one.

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

### Preview a Preset

```http
GET /api/marketplace-demo-presets/info/{name}/
Authorization: Token <staff_token>
```

Shows what loading the preset would do, before anything is deleted. Besides
the metadata and entity counts that `list` returns, it carries:

| Field | Content |
|---|---|
| `users` | Username, email, name, staff, support and active flags, roles with the name of their scope, and the password for active users who are neither staff nor support |
| `organizations` | Organizations, their projects, and each project's resources |
| `offerings` | Offering names grouped by type and category |
| `settings_changes` | Settings the load changes, and every setting the preset sets |
| `feature_changes` | Features the load changes, and every feature the preset sets |
| `overlay`, `overlay_contents` | The deployment's overlay preset imported after this one, if any, and what it adds |
| `warnings` | What would not work after the load |

Setting and feature changes describe a load with cleanup, the default:

- **Values after the load.** Each change has the current value and the value
  after the load.
- **`source`.** Where the new value comes from: `preset`, `overlay`,
  `default` (the load resets it), or `deployment` (the deployment owns the
  setting, so the preset's value is skipped and `skipped` is true).
- **Secrets.** A secret's values are always null.

Warnings name unknown settings or features in the preset, setting values
the load would reject (those keep their default), and an overlay that is
missing or cannot be read. They also name features turned on without the
configuration they need: the AI assistant without `AI_ASSISTANT_API_URL` and
`AI_ASSISTANT_API_TOKEN`, or Matrix chat without `MATRIX_ENABLED`,
`MATRIX_HOMESERVER_URL` and `MATRIX_APPSERVICE_AS_TOKEN`. This is checked
against the values after the load: what the deployment owns, plus what the
preset and overlay set. Anything else set today is reset by the load and
does not count.

`overlay_contents` lists the users, organizations and offerings the overlay
adds, never with passwords. Users marked inactive in a preset are listed with
`is_active: false` and no password.

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

## Deployment Presets

A deployment can add presets of its own, keep what it needs across every
load, and stop the published staff and support passwords from working. All three are set
in the environment only, and apply to the API and the `waldur demo_presets`
command alike.

| Variable | `WALDUR_CORE` setting | Effect |
|---|---|---|
| `WALDUR_DEMO_PRESETS_EXTRA_DIRS` | `DEMO_PRESETS_EXTRA_DIRS` | Comma-separated directories with more preset files |
| `WALDUR_DEMO_PRESETS_OVERLAY` | `DEMO_PRESETS_OVERLAY` | Preset imported after every load that cleans up first |
| `WALDUR_DEMO_PRESETS_STAFF_PASSWORD` | `DEMO_PRESETS_STAFF_PASSWORD` | Password the preset's staff and support users get |

### Extra Directories

Each `*.json` file in an extra directory is a preset named after the file.
The directories are searched in the order given, before the built-in one, so
a deployment's preset replaces a built-in preset of the same name. A missing
directory is logged and skipped.

### Overlay

The overlay holds what the deployment needs whatever the demo: offerings
backed by its own services, the users and API tokens of its agents, the
features it turns on. After a load that cleans up first, the overlay is
imported without cleanup, before orders, billing and credit history are
generated, so it is billed once with the rest. It is imported last, so its
settings and features win over the preset's.

- The API never lists, describes or loads the overlay.
- A load with `--no-cleanup` imports no overlay.
- If the overlay is set but its file is missing, the load fails before
  anything is deleted.

To keep an agent's token from expiring, give its user `"token_lifetime": null`;
without the key, a new user gets the deployment's default lifetime.

### Staff Password

When set, every staff and support user of the loaded preset and overlay
gets this password in place of the one in the preset file, which is public:
support reads every organisation, project, user and ticket. The API never
shows it, and the status endpoint never lists staff or support users. A load
with `skip_users` leaves passwords alone.

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
2. Place JSON file in `src/waldur_mastermind/marketplace/demo_presets/presets/`,
   or in one of the deployment's extra directories
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
