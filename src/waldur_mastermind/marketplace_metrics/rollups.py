"""Hourly and daily roll-ups of raw metric points.

A point can land in any bucket since the start of the day the late-data
window reaches back to: ingest refuses points older than that window, so
buckets before it never change again. A scheduled run recomputes only the
buckets that hold a point received since the previous run, which a late
point or a correction marks as well. Once a day, and whenever the previous
run is not known, every bucket of the window is recomputed instead, as a
safety net. Either way a roll-up row is written only when its figures
change.
"""

import datetime
import decimal

from django.db import connection, transaction
from django.utils import timezone

from waldur_core.configuration import config

from . import enums, models, retention

# An ingest transaction still open when a run starts commits after it, with
# points received before the run started: each run reaches back this much
# before the previous one started.
OVERLAP = datetime.timedelta(minutes=15)

STEPS = {
    enums.Granularities.HOUR: "1 hour",
    enums.Granularities.DAY: "1 day",
}

# The most a roll-up's sum holds, numeric(30,6). A bucket's sum is clamped to
# it: a reporter's huge values would otherwise fail every run, for every
# series, for as long as the bucket is in the window.
MAX_SUM = decimal.Decimal("999999999999999999999999.999999")

_AGGREGATES = """
    count(*) AS count,
    LEAST(GREATEST(sum(point.value), -%(max_sum)s), %(max_sum)s) AS sum,
    min(point.value) AS min,
    max(point.value) AS max,
    (array_agg(point.value ORDER BY point.timestamp DESC))[1] AS last,
    max(point.timestamp) AS last_timestamp
"""

# Only new and changed rows reach the INSERT: ON CONFLICT ... DO UPDATE locks
# and dirties a row even when its WHERE leaves the row as it was.
_WRITE = """
INSERT INTO {rollup} (
    series_id, granularity, bucket_start,
    count, sum, min, max, last, last_timestamp
)
SELECT
    computed.series_id, %(granularity)s, computed.bucket_start,
    computed.count, computed.sum, computed.min, computed.max,
    computed.last, computed.last_timestamp
FROM computed
LEFT JOIN {rollup} existing
    ON existing.series_id = computed.series_id
    AND existing.granularity = %(granularity)s
    AND existing.bucket_start = computed.bucket_start
WHERE existing.id IS NULL OR (
    existing.count, existing.sum, existing.min, existing.max,
    existing.last, existing.last_timestamp
) IS DISTINCT FROM (
    computed.count, computed.sum, computed.min, computed.max,
    computed.last, computed.last_timestamp
)
ON CONFLICT (series_id, granularity, bucket_start) DO UPDATE SET
    count = EXCLUDED.count,
    sum = EXCLUDED.sum,
    min = EXCLUDED.min,
    max = EXCLUDED.max,
    last = EXCLUDED.last,
    last_timestamp = EXCLUDED.last_timestamp
"""

# Every bucket from since.
WINDOW_SQL = (
    """
WITH computed AS (
    SELECT
        point.series_id,
        date_trunc(%(granularity)s, point.timestamp AT TIME ZONE 'UTC')
            AT TIME ZONE 'UTC' AS bucket_start,
"""
    + _AGGREGATES
    + """
    FROM {point} point
    WHERE point.timestamp >= %(since)s
    GROUP BY 1, 2
)
"""
    + _WRITE
)

# The buckets from since that hold a point received since received_since.
TOUCHED_SQL = (
    """
WITH touched AS (
    SELECT DISTINCT
        series_id,
        date_trunc(%(granularity)s, timestamp AT TIME ZONE 'UTC')
            AT TIME ZONE 'UTC' AS bucket_start
    FROM {point}
    WHERE received_at > %(received_since)s AND timestamp >= %(since)s
),
computed AS (
    SELECT
        point.series_id,
        touched.bucket_start,
"""
    + _AGGREGATES
    + """
    FROM touched
    JOIN {point} point
        ON point.series_id = touched.series_id
        AND point.timestamp >= touched.bucket_start
        AND point.timestamp < touched.bucket_start + %(step)s::interval
    GROUP BY point.series_id, touched.bucket_start
)
"""
    + _WRITE
)


def _floor_day(moment):
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def recompute_since():
    now = timezone.now()
    since = _floor_day(now - datetime.timedelta(days=config.METRICS_LATE_DATA_DAYS))
    # Never reach back into days whose raw points retention has started to
    # delete: recomputing such a day from what is left would overwrite its
    # complete roll-up with a partial one.
    raw_days = [p.raw_days for p in models.RetentionPolicy.objects.all()]
    raw_days.append(retention.default_policy().raw_days)
    oldest_complete = _floor_day(now - datetime.timedelta(days=min(raw_days)))
    oldest_complete += datetime.timedelta(days=1)
    return max(since, oldest_complete)


def roll_up(since=None, whole_window=False):
    """Bring the roll-ups up to date with the points; return the rows written.

    Given since, every bucket from then is recomputed; with whole_window,
    every bucket of the window. Otherwise only the buckets holding a point
    received since the previous run are, or the whole window when the
    previous run is not known; and such a run gives way to one already going,
    as the next starts from where that one started.
    """
    window = recompute_since()
    started = timezone.now()
    written = 0
    tables = dict(
        rollup=models.MetricRollup._meta.db_table,
        point=models.MetricPoint._meta.db_table,
    )
    scheduled = since is None and not whole_window
    with transaction.atomic():
        models.RollupState.objects.get_or_create(pk=1)
        # One run at a time. The daily and explicit ones wait for their turn.
        state = (
            models.RollupState.objects.select_for_update(skip_locked=scheduled)
            .filter(pk=1)
            .first()
        )
        if state is None:
            return 0
        incremental = scheduled and state.rolled_up_until is not None
        with connection.cursor() as cursor:
            for granularity in enums.Granularities.ROLLUPS:
                params = {"granularity": granularity, "max_sum": MAX_SUM}
                if incremental:
                    cursor.execute(
                        TOUCHED_SQL.format(**tables),
                        {
                            **params,
                            "step": STEPS[granularity],
                            "since": window,
                            "received_since": state.rolled_up_until - OVERLAP,
                        },
                    )
                else:
                    cursor.execute(
                        WINDOW_SQL.format(**tables),
                        {**params, "since": since or window},
                    )
                written += cursor.rowcount
        # A run from later than the window's start leaves points out, so the
        # next one cannot start from it.
        if since is None or since <= window:
            state.rolled_up_until = started
            state.save(update_fields=["rolled_up_until"])
    return written
