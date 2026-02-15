# Arrow (ArrowSphere) Integration

## Overview

The Arrow integration connects Waldur with the ArrowSphere cloud marketplace platform. It imports
billing data, tracks consumption, and reconciles costs for IaaS subscriptions (Azure, AWS, etc.)
managed through Arrow as a channel partner.

The integration provides:

- Customer mapping between Arrow references (XSP...) and Waldur organizations
- Vendor-to-offering mapping (e.g., Microsoft to a Waldur Azure offering)
- Billing export sync with invoice item creation
- Real-time consumption tracking with finalized billing reconciliation
- Resource import from Arrow subscriptions

## Prerequisites

- Active ArrowSphere partner account with API access
- Arrow API key with permissions for billing, customers, subscriptions, and consumption endpoints
- Feature flag `reseller.arrow` enabled in Waldur

## Configuration

### Feature Flag

Enable the Arrow integration in Waldur features:

```python
# Via Django admin or API
FEATURES["reseller.arrow"] = True
```

### Constance Settings

All settings are managed via Constance (runtime-configurable):

| Setting | Default | Description |
|---------|---------|-------------|
| `ARROW_AUTO_RECONCILIATION` | `False` | Auto-apply compensations when Arrow validates billing |
| `ARROW_SYNC_INTERVAL_HOURS` | `6` | Billing sync interval in hours |
| `ARROW_CONSUMPTION_SYNC_ENABLED` | `False` | Enable real-time consumption sync from Arrow API |
| `ARROW_CONSUMPTION_SYNC_INTERVAL_HOURS` | `1` | Consumption sync interval in hours |
| `ARROW_BILLING_CHECK_INTERVAL_HOURS` | `6` | Billing export check interval for reconciliation |

### Arrow Settings Record

Arrow API credentials are stored in the `ArrowSettings` model, not in Constance. Create settings
via the API or setup wizard (see Setup Workflow below). Key fields:

| Field | Description |
|-------|-------------|
| `api_url` | Arrow API base URL (e.g., `https://xsp.arrow.com/index.php/api/`) |
| `api_key` | API key for authentication |
| `export_type_reference` | Billing export template reference (discovered from API) |
| `invoice_price_source` | Which price to use for invoice items: `sell` (default) or `buy` |
| `sync_enabled` | Whether automatic billing sync is enabled |
| `is_active` | Whether this settings record is active |

Only one active settings record should exist per deployment.

## Setup Workflow

### Step 1: Enable Feature Flag

Enable `reseller.arrow` via the features API or Django admin.

### Step 2: Validate and Save API Credentials

Use the setup wizard endpoints to configure credentials:

```text
POST /api/admin/arrow/settings/validate_credentials/
{
    "api_url": "https://xsp.arrow.com/index.php/api/",
    "api_key": "your-api-key"
}
```

This validates the credentials and returns partner info and available export types.

Then save the settings:

```text
POST /api/admin/arrow/settings/save_settings/
{
    "api_url": "https://xsp.arrow.com/index.php/api/",
    "api_key": "your-api-key",
    "export_type_reference": "DJ284LDZ-standard",
    "sync_enabled": true,
    "customer_mappings": [
        {"arrow_reference": "XSP661245", "waldur_customer_uuid": "..."}
    ]
}
```

### Step 3: Discover and Map Customers

Discover Arrow customers and get mapping suggestions:

```text
POST /api/admin/arrow/settings/discover_customers/
{
    "api_url": "https://xsp.arrow.com/index.php/api/",
    "api_key": "your-api-key"
}
```

The response includes:

- Fuzzy-matched suggestions between Arrow company names and Waldur organizations
- Export type compatibility information showing which export types have the required and important fields used by Waldur (see Export Type Compatibility below)

Create mappings via:

```text
POST /api/admin/arrow/customer-mappings/
{
    "settings": "<settings-uuid>",
    "arrow_reference": "XSP661245",
    "arrow_company_name": "Example Corp",
    "waldur_customer": "<customer-uuid>"
}
```

Alternatively, use `sync_from_arrow` to bulk-sync customer data:

```text
POST /api/admin/arrow/customer-mappings/sync_from_arrow/
```

### Step 4: Map Vendors to Offerings

Map Arrow vendor names to Waldur marketplace offerings and plans:

```text
POST /api/admin/arrow/vendor-offering-mappings/
{
    "settings": "<settings-uuid>",
    "arrow_vendor_name": "Microsoft",
    "offering": "<offering-uuid>",
    "plan": "<plan-uuid>"
}
```

The `plan` field is mandatory and must reference a plan belonging to the selected offering. Use the `vendor_choices` endpoint to list available vendor names:

```text
GET /api/admin/arrow/vendor-offering-mappings/vendor_choices/
```

### Step 5: Sync Billing Data

Trigger a billing sync for a specific period:

```text
POST /api/admin/arrow/billing-syncs/trigger_sync/
{
    "year": 2026,
    "month": 1
}
```

### Step 6: Enable Consumption Tracking (Optional)

For real-time consumption tracking, set `ARROW_CONSUMPTION_SYNC_ENABLED` to `True` in Constance.
This enables hourly consumption sync from the Arrow Consumption API.

Resources must have their `backend_id` field set to the Arrow license reference (e.g., `XSP12345`).
The license reference is stored in the resource's `arrow_license_reference` attribute.
Use the `discover_licenses` and `link_resource` actions on customer mappings to link resources.

During consumption sync, resources are matched by `ARS Subscription ID` from the Arrow billing export.

## API Endpoints

All endpoints require staff permissions and are located under `/api/admin/arrow/`.

### Settings (`/api/admin/arrow/settings/`)

Standard CRUD plus:

| Action | Method | Description |
|--------|--------|-------------|
| `validate_credentials` | POST | Test API credentials without saving |
| `discover_customers` | POST | Fetch Arrow customers with mapping suggestions and export type compatibility |
| `preview_settings` | POST | Preview settings before saving |
| `save_settings` | POST | Save settings and optionally create customer mappings |

### Customer Mappings (`/api/admin/arrow/customer-mappings/`)

Standard CRUD plus:

| Action | Method | Description |
|--------|--------|-------------|
| `sync_from_arrow` | POST | Bulk-sync customer data from Arrow API |
| `billing_summary` | GET (detail) | View billing summary for a mapped customer |
| `fetch_arrow_data` | GET (detail) | Fetch fresh billing and consumption data from Arrow |
| `discover_licenses` | GET (detail) | Discover Arrow licenses and suggest resource links |
| `link_resource` | POST (detail) | Link a Waldur resource to an Arrow license |
| `import_license` | POST (detail) | Import an Arrow license as a new Waldur resource |
| `available_customers` | GET | List unmapped Arrow customers with suggestions |

### Vendor Offering Mappings (`/api/admin/arrow/vendor-offering-mappings/`)

Standard CRUD plus:

| Action | Method | Description |
|--------|--------|-------------|
| `vendor_choices` | GET | List available Arrow vendor names |

### Billing Syncs (`/api/admin/arrow/billing-syncs/`)

Read-only list/retrieve plus:

| Action | Method | Description |
|--------|--------|-------------|
| `trigger_sync` | POST | Trigger billing sync for a specific month |
| `reconcile` | POST | Trigger reconciliation for a specific month |
| `sync_resources` | POST | Sync Arrow subscriptions to Waldur resources |
| `trigger_consumption_sync` | POST | Trigger consumption sync for a specific month |
| `sync_resource_historical_consumption` | POST | Sync historical consumption for a resource |
| `trigger_reconciliation` | POST | Trigger billing export check and reconciliation |
| `cleanup_consumption` | POST | Clean up consumption records |
| `pause_sync` | POST | Pause automatic sync |
| `resume_sync` | POST | Resume automatic sync |
| `consumption_status` | GET | View consumption sync status |
| `consumption_statistics` | GET | View consumption statistics |
| `pending_records` | GET | List pending consumption records |
| `fetch_consumption` | POST | Fetch raw consumption data from Arrow API |
| `fetch_billing_export` | POST | Fetch raw billing export from Arrow API |
| `fetch_license_info` | POST | Fetch license details from Arrow API |

### Consumption Records (`/api/admin/arrow/consumption-records/`)

Read-only list/retrieve. Filterable by `resource_uuid`, `customer_uuid`, `project_uuid`,
`license_reference`, `billing_period`, `is_finalized`, and `is_reconciled`.

### Billing Sync Items (`/api/admin/arrow/billing-sync-items/`)

Read-only list/retrieve. Filterable by `billing_sync_uuid`, `report_period`, `vendor_name`,
`classification`, and `has_compensation`.

## Periodic Tasks

The following Celery tasks run automatically:

| Task | Default Interval | Description |
|------|-----------------|-------------|
| `sync-arrow-billing` | Every 6 hours | Syncs billing export for the current month |
| `check-arrow-validated-billing` | Every 12 hours | Checks for validated billing and triggers reconciliation |
| `sync-arrow-consumption` | Every 1 hour | Syncs real-time consumption data (requires `ARROW_CONSUMPTION_SYNC_ENABLED`) |
| `check-arrow-billing-export` | Every 6 hours | Checks billing export and reconciles consumption records |

All tasks check `ArrowSettings.sync_enabled` before running. Consumption tasks additionally
check `ARROW_CONSUMPTION_SYNC_ENABLED`.

## Management Commands

### sync_arrow_resources

Sync Arrow IAAS subscriptions to Waldur resources from the command line:

```bash
waldur sync_arrow_resources
```

**Arguments**:

| Argument | Description |
|----------|-------------|
| `--period-from` | Start period in YYYY-MM format (default: 6 months ago) |
| `--period-to` | End period in YYYY-MM format (default: current month) |
| `--customer-uuid` | Waldur Customer UUID to create resources under |
| `--project-uuid` | Waldur Project UUID to create resources under |
| `--dry-run` | Preview changes without modifying the database |
| `--create-offering` | Create an Arrow Azure offering if none exists |
| `--force-import` | Auto-create Waldur Customers and Projects from Arrow data |

**Example - dry run**:

```bash
waldur sync_arrow_resources --dry-run --period-from 2025-07 --period-to 2026-01
```

**Example - force import**:

```bash
waldur sync_arrow_resources --force-import
```

In force-import mode, the system:

1. Creates a Waldur Customer for each Arrow customer (with an "Arrow Azure Subscriptions" project)
2. Creates a usage-based plan ("Arrow Cloud Cost") on the offering with a `cloud_cost` component
3. Assigns each resource to this plan and creates a `ResourcePlanPeriod` record
4. Records `ComponentUsage` for each billing period, enabling proper billing integration

## Invoice Price Source

The `invoice_price_source` setting controls which Arrow price is used for Waldur invoice items:

- **`sell`** (default): Uses sell/customer prices from Arrow billing data
- **`buy`**: Uses buy/wholesale prices from Arrow billing data

This affects:

- Provisional invoice items created during consumption sync
- Reconciliation adjustments when finalized billing arrives
- Billing export invoice item creation

## Billing Export Field Mapping

Different Arrow export types use different column names. The system handles this with fallback
chains -- it tries the first field name and falls back to alternatives:

| Waldur Field | Primary Column | Fallback Column(s) |
|-------------|----------------|---------------------|
| Line reference | `Sequence` | `Order Id` |
| Sell price | `Sell Total Price` | `Customer Total Price` |
| Buy price | `Buy Total Price` | `Total Wholesale Price` |
| Quantity | `Quantity` | `Qty` (defaults to 1) |
| Vendor | `Vendor Name` | `Service Name` |
| Product | `Product Name` | `Friendly Name` -> `Description` |
| License reference | `ARS Subscription ID` | -- |
| Customer grouping | `End User Company Name` | -- |
| Subscription reference | `Vendor Subscription ID` | -- |
| SKU | `Arrow SKU` | -- |

### Customer Grouping

Billing lines are grouped by `End User Company Name`, matched against
`ArrowCustomerMapping.arrow_company_name`. Each mapping's `arrow_company_name` field must
match the company name that appears in the billing export for that customer.

### Export Type Compatibility

The `discover_customers` endpoint checks each available export type for compatibility by
inspecting its column headers. For each export type, the response includes:

- `compatible`: Whether all required fields are present
- `recommended`: Whether the export type has both required and most important fields
- `missing_required_fields`: List of missing required fields
- `missing_important_fields`: List of missing important fields

The compatible export type is typically "MSP (Extended)". Choose an export type where
`compatible` is `true` and `recommended` is `true` for best results.

## Billing Sync Lifecycle

Arrow billing sync records follow a finite-state machine:

```mermaid
graph LR
    A[Pending] --> B[Synced]
    B --> C[Validated]
    C --> D[Reconciled]
```

| State | Description |
|-------|-------------|
| **Pending** | Billing sync record created, waiting for data |
| **Synced** | Billing data fetched from Arrow and invoice items created |
| **Validated** | Arrow has validated the billing statement |
| **Reconciled** | Compensation items applied for any price differences |

### Reconciliation

When billing moves from Synced to Validated, the system can automatically apply compensations
if `ARROW_AUTO_RECONCILIATION` is enabled. Otherwise, use the `reconcile` action to trigger
reconciliation manually.

During reconciliation, license references are matched by `ARS Subscription ID`. The price
fields used for comparison depend on which columns are present in the export (see Field
Mapping above).

### Consumption-Based Reconciliation

When consumption tracking is enabled, the flow is:

1. Hourly consumption data is synced from Arrow's Consumption API (provisional amounts)
2. Invoice items are created/updated with consumed amounts (using the configured `invoice_price_source`)
3. When Arrow's finalized billing export arrives, final amounts are compared
4. If final amounts differ from consumed amounts, compensation items are created
5. Licenses with zero consumption (both sell and buy are 0) are automatically skipped

## Troubleshooting

### No active Arrow settings found

Ensure an `ArrowSettings` record exists with `is_active=True`. Create one via the API or
setup wizard.

### Billing sync runs but creates no items

- Verify customer mappings exist and are active
- Ensure `arrow_company_name` is set correctly on customer mappings -- billing lines are matched by company name
- Check that `export_type_reference` is set correctly (use `discover_customers` to see export type compatibility)
- Use an export type where `compatible` is `true` (typically "MSP (Extended)")
- Different export types use different column names (e.g., `Customer Total Price` vs `Sell Total Price`). The system handles this with fallback chains, but check logs for warnings.

### Consumption sync not running

- Set `ARROW_CONSUMPTION_SYNC_ENABLED` to `True` in Constance
- Ensure `ArrowSettings.sync_enabled` is `True`
- Verify resources have `backend_id` set to Arrow license references

### Resources not linked to Arrow licenses

Use the `discover_licenses` action on a customer mapping to find unlinked licenses, then
use `link_resource` to set the `backend_id` on existing resources or `import_license` to
create new ones.

### API authentication failures

- Verify the API key is valid using `validate_credentials`
- Check that the Arrow API URL includes the correct base path (e.g., `https://xsp.arrow.com/index.php/api/`)
- Review Waldur logs for detailed error messages from the Arrow API

## Related Files

- Models: `src/waldur_mastermind/waldur_arrow/models.py`
- Backend client: `src/waldur_mastermind/waldur_arrow/backend.py`
- Views: `src/waldur_mastermind/waldur_arrow/views.py`
- Tasks: `src/waldur_mastermind/waldur_arrow/tasks.py`
- Extension: `src/waldur_mastermind/waldur_arrow/extension.py`
- CLI command: `src/waldur_mastermind/waldur_arrow/management/commands/sync_arrow_resources.py`
