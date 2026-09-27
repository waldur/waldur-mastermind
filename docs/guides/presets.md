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
| `call_management` | Calls for proposals, peer review, conflict-of-interest detection, allocation |
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

Response includes user credentials:

```json
{
  "success": true,
  "message": "Preset 'minimal_quickstart' loaded successfully",
  "output": "...",
  "users": [
    {"username": "staff", "password": "demo", "is_staff": true, "is_support": false},
    {"username": "owner", "password": "demo", "is_staff": false, "is_support": false}
  ]
}
```

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
