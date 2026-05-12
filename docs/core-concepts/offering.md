# Offering Configuration

An **Offering** represents a service or product that can be ordered through the Waldur marketplace. This document describes the configuration options available for offerings.

## Overview

Offerings are created by service providers and define:

- What service is being offered (type, description, terms)
- How users can customize their orders (options)
- How provisioned resources can be modified (resource_options)
- Behavioral rules and constraints (plugin_options)
- Pricing structure (plans and components)

## Data Flow: Options to Resource

Understanding how user input flows through the system:

```mermaid
flowchart LR
    subgraph Offering["Offering (schema)"]
        OO["options"]
        RO["resource_options"]
    end

    subgraph Order["Order"]
        OA["attributes"]
    end

    subgraph Resource["Resource"]
        RA["attributes"]
        ROPT["options"]
    end

    OO -->|defines form| OA
    OA -->|all values| RA
    OA -->|filtered by| RO
    RO -->|matching keys| ROPT

    style RA fill:#e1f5fe
    style ROPT fill:#c8e6c9
```

| Step | What happens |
|------|--------------|
| 1 | **`offering.options`** defines the order form schema |
| 2 | User fills out the form, values become **`order.attributes`** |
| 3 | All attributes are copied to **`resource.attributes`** (immutable) |
| 4 | Only attributes matching keys in **`offering.resource_options`** are copied to **`resource.options`** |
| 5 | **`resource.options`** can be modified after provisioning (triggers UPDATE orders) |

## Key Configuration Fields

### options

Defines the input fields users fill out when creating an order. These values are stored in `order.attributes` and `resource.attributes`.

```json
{
  "options": {
    "order": ["storage_data_type", "permissions", "hard_quota_space"],
    "options": {
      "storage_data_type": {
        "type": "select_string",
        "label": "Storage Type",
        "required": true,
        "choices": ["Store", "Archive", "Scratch"]
      },
      "permissions": {
        "type": "select_string",
        "label": "Permissions",
        "required": true,
        "choices": ["2770", "2775", "2777"]
      },
      "hard_quota_space": {
        "type": "integer",
        "label": "Space (TB)",
        "required": true
      }
    }
  }
}
```

**Supported field types:**

| Type | Description |
|------|-------------|
| `string` | Free text input |
| `text` | Multi-line text input |
| `integer` | Whole number |
| `money` | Decimal number for currency |
| `boolean` | True/false checkbox |
| `select_string` | Dropdown with string choices |
| `select_string_multi` | Multi-select dropdown |
| `date` | Date picker |
| `time` | Time picker |
| `html_text` | Rich text editor |
| `component_multiplier` | Links to component for billing |

### resource_options

Defines which attributes can be modified after resource creation. When an order is created, attribute values matching keys defined here are copied to `resource.options`.

**Important:** The keys in `resource_options.options` act as a filter. Only attributes with matching keys are copied to `resource.options` and become modifiable.

```json
{
  "resource_options": {
    "order": ["soft_quota_space", "hard_quota_space", "permissions"],
    "options": {
      "soft_quota_space": {
        "type": "integer",
        "label": "Soft Quota (TB)",
        "required": false
      },
      "hard_quota_space": {
        "type": "integer",
        "label": "Hard Quota (TB)",
        "required": false
      },
      "permissions": {
        "type": "select_string",
        "label": "Permissions",
        "required": false,
        "choices": ["2770", "2775", "2777"]
      }
    }
  }
}
```

**Example flow:**

1. User orders with: `storage_data_type=Store`, `permissions=2770`, `hard_quota_space=10`
2. `resource.attributes` = `{storage_data_type: "Store", permissions: "2770", hard_quota_space: 10}`
3. `resource.options` = `{permissions: "2770", hard_quota_space: 10}` (only keys from `resource_options`)
4. `storage_data_type` is NOT in `resource.options` because it's not in `resource_options.options`
5. User can later modify `permissions` and `hard_quota_space`, but NOT `storage_data_type`

### plugin_options

Defines behavioral rules, constraints, and provider-specific settings. This is where most operational configuration lives.

### backend_id_rules

Defines per-offering validation rules for the `backend_id` field on resources. Supports format validation via regex and configurable uniqueness scopes. Default is `{}` (no validation, backward compatible). Empty `backend_id` values always bypass validation.

```json
{
  "backend_id_rules": {
    "format": {
      "regex": "^[A-Z]{2}-\\d{6}$",
      "description": "Must be 2 uppercase letters, dash, 6 digits"
    },
    "uniqueness": {
      "scope": "offering",
      "include_terminated": false
    }
  }
}
```

Both `format` and `uniqueness` are optional top-level keys.

**Format validation:**

| Field | Type | Description |
|-------|------|-------------|
| `format.regex` | string | Python regex pattern validated with `re.fullmatch`. Max 200 characters. Patterns with nested/adjacent quantifiers are rejected (ReDoS protection) |
| `format.description` | string | Human-readable description shown in validation errors. Falls back to displaying the regex pattern |

**Uniqueness configuration:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `uniqueness.scope` | string | — | Scope for uniqueness check (see table below) |
| `uniqueness.include_terminated` | boolean | `true` | Whether terminated resources are included in the uniqueness check |

**Uniqueness scopes:**

| Scope | Description |
|-------|-------------|
| `offering` | Unique across resources of this offering |
| `offering_group` | Unique across all offerings that share the same `offering.backend_id` (e.g. offerings attached to the same vcluster). Falls back to `offering` scope if the offering has no `backend_id` |
| `service_provider` | Unique across all offerings of the same customer/service provider |
| `service_provider_category` | Unique across all offerings of the same provider in the same category |

**API endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/marketplace-provider-offerings/{uuid}/update_backend_id_rules/` | POST | Configure rules. Requires `UPDATE_OFFERING_OPTIONS` permission |
| `/api/marketplace-provider-offerings/{uuid}/check_unique_backend_id/` | POST | Check a backend ID. Set `use_offering_rules: true` to validate format and uniqueness per configured rules |

**Enforcement points:**

- `set_backend_id` action (manual backend ID assignment)
- `import_resource` action (resource import from external systems)
- Not applied when backend systems automatically set `backend_id` via processors

**Visibility:** `backend_id_rules` is exposed on the provider offering serializer but excluded from the public offering serializer.

## Plugin Options Reference

### Approval and Auto-Processing

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `auto_approve_remote_orders` | boolean | `false` | Skip provider approval for orders from external customers |
| `auto_approve_in_service_provider_projects` | boolean | `false` | Skip consumer approval when ordering within the same organization |
| `disable_autoapprove` | boolean | `false` | Force manual approval for all orders, overriding other auto-approve settings |

**Example:**

```json
{
  "plugin_options": {
    "auto_approve_remote_orders": true,
    "auto_approve_in_service_provider_projects": true
  }
}
```

### Resource Constraints

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `maximal_resource_count_per_project` | integer | none | Maximum number of resources from this offering per project |
| `unique_resource_per_attribute` | string | none | Attribute name to enforce uniqueness. Only one non-terminated resource per attribute value per project |
| `minimal_team_count_for_provisioning` | integer | none | Minimum number of team members required in project |
| `required_team_role_for_provisioning` | string | none | Required role name (e.g., "PI") for user to provision |

**Example - Storage offering with one resource per storage type:**

```json
{
  "plugin_options": {
    "unique_resource_per_attribute": "storage_data_type",
    "maximal_resource_count_per_project": 4
  }
}
```

With this configuration:

- A project can have one "Store", one "Archive", one "Users", and one "Scratch" resource
- A project cannot have two "Store" resources (blocked by `unique_resource_per_attribute`)
- Total resources capped at 4 (defense in depth via `maximal_resource_count_per_project`)

### Resource Lifecycle

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `is_resource_termination_date_required` | boolean | `false` | Require end date when ordering |
| `default_resource_termination_offset_in_days` | integer | none | Default days until termination from order date |
| `max_resource_termination_offset_in_days` | integer | none | Maximum days until termination allowed |
| `latest_date_for_resource_termination` | date | none | Hard deadline for all resource terminations |
| `resource_expiration_threshold` | integer | `30` | Days before expiration to start warning users |
| `can_restore_resource` | boolean | `false` | Allow restoring terminated resources |
| `supports_downscaling` | boolean | `false` | Allow reducing resource limits |
| `supports_pausing` | boolean | `false` | Allow pausing/resuming resources |
| `restrict_deletion_with_active_resources` | boolean | `false` | Prevent offering deletion while it has non-terminated resources (applies to all users including staff) |

**Example:**

```json
{
  "plugin_options": {
    "is_resource_termination_date_required": true,
    "default_resource_termination_offset_in_days": 90,
    "max_resource_termination_offset_in_days": 365,
    "restrict_deletion_with_active_resources": true
  }
}
```

### Order Processing

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `create_orders_on_resource_option_change` | boolean | `false` | Create UPDATE orders when resource_options change |
| `enable_purchase_order_upload` | boolean | `false` | Allow users to attach purchase orders |
| `require_purchase_order_upload` | boolean | `false` | Require purchase order attachment |
| `enable_provider_consumer_messaging` | boolean | `false` | Allow providers and consumers to exchange messages with attachments on pending orders |
| `notify_about_provider_consumer_messages` | boolean | `false` | Send email notifications when providers or consumers exchange messages on pending orders. Requires `enable_provider_consumer_messaging` |

### Resource Naming

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `resource_name_pattern` | string | none | Python format string for generating suggested resource names |

When set, the `suggest_name` endpoint uses this pattern instead of the default `{customer_slug}-{project_slug}-{offering_slug}[-counter]` format.

**Available variables:**

| Variable | Description |
|----------|-------------|
| `{customer_name}` | Customer organization name |
| `{customer_slug}` | Customer slug |
| `{project_name}` | Project name |
| `{project_slug}` | Project slug |
| `{offering_name}` | Offering name |
| `{offering_slug}` | Offering slug |
| `{plan_name}` | Selected plan name (empty if no plan provided) |
| `{counter}` | Incremental counter (empty for first resource, `2` for second, etc.) |
| `{attributes[KEY]}` | Any order form attribute value (empty if the key is missing) |

**Examples:**

```json
{
  "plugin_options": {
    "resource_name_pattern": "{project_slug}-{offering_slug}-{counter}"
  }
}
```

With attributes from the order form:

```json
{
  "plugin_options": {
    "resource_name_pattern": "{project_slug}-{attributes[environment]}-{counter}"
  }
}
```

Non-alphanumeric characters (except `-`, `_`, `.`) are replaced with hyphens; duplicate hyphens are collapsed; leading/trailing hyphens are stripped. If the pattern is malformed, the endpoint falls back to the default naming behavior.

### Display and UI

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `conceal_billing_data` | boolean | `false` | Hide pricing/billing information from users |
| `highlight_backend_id_display` | boolean | `false` | Emphasize backend ID in resource display |
| `backend_id_display_label` | string | none | Custom label for backend ID field |

### Offering Users (Identity Management)

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `service_provider_can_create_offering_user` | boolean | `false` | Allow provider to create offering-specific user accounts |
| `username_generation_policy` | string | `"waldur_username"` | How usernames are generated: `waldur_username`, `anonymized`, `service_provider`, `full_name`, `freeipa`, `eduteams` |
| `initial_uidnumber` | integer | `5000` | Starting UID for generated users |
| `initial_primarygroup_number` | integer | `5000` | Starting GID for primary groups |
| `initial_usergroup_number` | integer | `6000` | Starting GID for user groups |
| `homedir_prefix` | string | `"/home/"` | Prefix for home directory paths |
| `username_anonymized_prefix` | string | `"walduruser_"` | Prefix for anonymized usernames |

## Plugin-Specific Options

### OpenStack

| Option | Type | Description |
|--------|------|-------------|
| `default_internal_network_mtu` | integer (68-9000) | MTU for tenant internal networks |
| `max_instances` | integer | Default instance limit per tenant |
| `max_volumes` | integer | Default volume limit per tenant |
| `max_security_groups` | integer | Default security group limit |
| `storage_mode` | `"fixed"` or `"dynamic"` | How storage quota is calculated |
| `snapshot_size_limit_gb` | integer | Snapshot size limit in GB |

### HEAppE (HPC)

| Option | Type | Description |
|--------|------|-------------|
| `heappe_url` | URL | HEAppE server endpoint |
| `heappe_username` | string | Service account username |
| `heappe_password` | string | Service account password |
| `heappe_cluster_id` | integer | Target cluster ID |
| `project_permanent_directory` | string | Persistent project directory path |
| `scratch_project_directory` | string | Temporary scratch directory path |

### GLAuth (LDAP)

| Option | Type | Description |
|--------|------|-------------|
| `glauth_records_path` | string | Path to GLAuth user records |
| `glauth_users_path` | string | Path to GLAuth users configuration |

### Rancher (Kubernetes)

See [Rancher plugin documentation](../plugins/rancher.md#offering-configuration-plugin_options) for detailed Rancher-specific options.

## Complete Example

A storage offering with comprehensive configuration:

```json
{
  "name": "HPC Storage",
  "type": "Marketplace.Slurm",
  "options": {
    "order": ["storage_data_type", "hard_quota_space"],
    "options": {
      "storage_data_type": {
        "type": "select_string",
        "label": "Storage Type",
        "required": true,
        "choices": ["Store", "Archive", "Users", "Scratch"]
      },
      "hard_quota_space": {
        "type": "integer",
        "label": "Space (TB)",
        "required": true
      }
    }
  },
  "resource_options": {
    "order": ["soft_quota_space", "hard_quota_space"],
    "options": {
      "soft_quota_space": {
        "type": "integer",
        "label": "Soft Quota (TB)"
      },
      "hard_quota_space": {
        "type": "integer",
        "label": "Hard Quota (TB)"
      }
    }
  },
  "plugin_options": {
    "disable_autoapprove": true,
    "unique_resource_per_attribute": "storage_data_type",
    "maximal_resource_count_per_project": 4,
    "is_resource_termination_date_required": true,
    "default_resource_termination_offset_in_days": 90,
    "max_resource_termination_offset_in_days": 730,
    "create_orders_on_resource_option_change": true,
    "service_provider_can_create_offering_user": true
  }
}
```

## Validation Behavior

### Order Creation Validation

When an order is created, the following `plugin_options` are validated:

1. **`maximal_resource_count_per_project`**: Counts non-terminated resources for the project+offering
2. **`unique_resource_per_attribute`**: Checks if a non-terminated resource with the same attribute value exists
3. **`minimal_team_count_for_provisioning`**: Validates project team size
4. **`required_team_role_for_provisioning`**: Validates user has required role

### Backend ID Validation

When `backend_id_rules` is configured on the offering, the following checks run on `set_backend_id` and `import_resource`:

1. If `backend_id` is empty, all validation is skipped
2. **Format check**: If `format.regex` is set, the value must match using `re.fullmatch`
3. **Uniqueness check**: If `uniqueness.scope` is set, a duplicate query runs against the configured scope

The `check_unique_backend_id` endpoint performs the same checks when `use_offering_rules` is `true`, returning `is_unique` and `is_valid_format` fields in the response.

### Approval Flow

The approval flow is determined by:

1. If `disable_autoapprove` is `true`, manual approval is always required
2. If ordering within same organization and `auto_approve_in_service_provider_projects` is `true`, consumer approval is skipped
3. If `auto_approve_remote_orders` is `true`, provider approval is skipped for external customers
4. Staff users bypass most approval requirements
