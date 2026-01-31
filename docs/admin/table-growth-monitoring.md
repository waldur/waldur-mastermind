# Table Growth Monitoring

Waldur includes a database table growth monitoring system that tracks PostgreSQL table sizes
over time. It detects abnormal growth patterns that may indicate bugs causing unbounded data
accumulation.

## Overview

The system consists of three components:

1. **Daily data collection** - A Celery task samples table sizes from PostgreSQL
2. **Historical storage** - The `DailyTableSizeHistory` model stores daily snapshots
3. **Growth analysis API** - An endpoint computes growth rates and raises alerts

## How It Works

### Data Collection

The `sample_table_sizes` Celery task runs daily and:

1. Queries PostgreSQL system catalogs for all user tables above a configurable size threshold
2. Records total size (including indexes), data-only size, and estimated row count
3. Stores one entry per table per day using `update_or_create`
4. Purges entries older than the configured retention period

The PostgreSQL query uses `pg_total_relation_size()`, `pg_relation_size()`, and
`pg_stat_user_tables.n_live_tup` to gather metrics.

### Growth Analysis

The API endpoint compares today's snapshot against snapshots from 7 and 30 days ago to
compute weekly and monthly growth percentages for both size and row count.

Growth percentage formula:

```text
growth_percent = (current_size - old_size) / old_size * 100
```

Tables are sorted by combined growth rate (weekly + monthly, descending). Alerts are
generated for any table exceeding the configured weekly or monthly threshold.

## Configuration

All settings are managed via Constance (runtime-configurable):

| Setting | Default | Description |
|---------|---------|-------------|
| `TABLE_GROWTH_MONITORING_ENABLED` | `True` | Master switch for the feature |
| `TABLE_GROWTH_MIN_SIZE_BYTES` | `1048576` (1 MB) | Minimum table size to track |
| `TABLE_GROWTH_RETENTION_DAYS` | `90` | Days of history to retain |
| `TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT` | `50` | Weekly growth % that triggers an alert |
| `TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT` | `200` | Monthly growth % that triggers an alert |

## API Endpoint

### Get Table Growth Statistics

```http
GET /api/stats/table-growth/
```

**Permissions**: Authenticated user with support or staff role.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `table_name` | string (optional) | Filter by table name (case-insensitive substring match) |

**Response**:

```json
{
  "date": "2026-01-31",
  "weekly_threshold_percent": 50,
  "monthly_threshold_percent": 200,
  "tables": [
    {
      "table_name": "marketplace_order",
      "current_total_size": 2000000,
      "current_data_size": 1500000,
      "current_row_estimate": 2000,
      "week_ago_total_size": 1000000,
      "week_ago_row_estimate": 1000,
      "month_ago_total_size": 500000,
      "month_ago_row_estimate": 500,
      "weekly_growth_percent": 100.0,
      "monthly_growth_percent": 300.0,
      "weekly_row_growth_percent": 100.0,
      "monthly_row_growth_percent": 300.0
    }
  ],
  "alerts": [
    {
      "table_name": "marketplace_order",
      "period": "weekly",
      "growth_percent": 100.0,
      "threshold": 50
    }
  ]
}
```

### Response Fields

**Top-level fields**:

| Field | Type | Description |
|-------|------|-------------|
| `date` | date | Current date of the statistics |
| `weekly_threshold_percent` | integer | Configured weekly alert threshold |
| `monthly_threshold_percent` | integer | Configured monthly alert threshold |
| `tables` | array | Table statistics sorted by growth rate (descending) |
| `alerts` | array | Tables that exceeded configured thresholds |

**Table entry fields**:

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `table_name` | string | no | Database table name |
| `current_total_size` | integer | no | Current total size in bytes (data + indexes) |
| `current_data_size` | integer | no | Current data-only size in bytes |
| `current_row_estimate` | integer | yes | Current estimated row count |
| `week_ago_total_size` | integer | yes | Total size 7 days ago |
| `week_ago_row_estimate` | integer | yes | Row estimate 7 days ago |
| `month_ago_total_size` | integer | yes | Total size 30 days ago |
| `month_ago_row_estimate` | integer | yes | Row estimate 30 days ago |
| `weekly_growth_percent` | float | yes | Size growth over 7 days (%) |
| `monthly_growth_percent` | float | yes | Size growth over 30 days (%) |
| `weekly_row_growth_percent` | float | yes | Row count growth over 7 days (%) |
| `monthly_row_growth_percent` | float | yes | Row count growth over 30 days (%) |

Growth fields are `null` when historical data is unavailable or the previous value was zero.

**Alert entry fields**:

| Field | Type | Description |
|-------|------|-------------|
| `table_name` | string | Table that triggered the alert |
| `period` | string | `"weekly"` or `"monthly"` |
| `growth_percent` | float | Actual growth percentage observed |
| `threshold` | integer | Threshold that was exceeded |

A single table can generate up to two alerts (one weekly, one monthly).

## Data Model

**Model**: `DailyTableSizeHistory`
**Location**: `src/waldur_core/core/models.py`

| Field | Type | Description |
|-------|------|-------------|
| `table_name` | CharField(150) | Database table name (indexed) |
| `date` | DateField | Snapshot date (indexed) |
| `total_size` | BigIntegerField | Total size including indexes in bytes |
| `data_size` | BigIntegerField | Data-only size in bytes |
| `row_estimate` | BigIntegerField (nullable) | Estimated row count |

**Constraints**: Unique on `(table_name, date)`.

## Celery Task

**Task name**: `waldur_core.sample_table_sizes`

The task is registered in the celerybeat schedule for daily execution. It can also be
triggered manually:

```python
from waldur_core.core.tasks import sample_table_sizes
sample_table_sizes.delay()
```

## Related Files

- Model: `src/waldur_core/core/models.py`
- Task: `src/waldur_core/core/tasks.py`
- API view: `src/waldur_core/core/views.py`
- Serializers: `src/waldur_core/core/serializers.py`
- Tests: `src/waldur_core/core/tests/test_table_growth.py`
