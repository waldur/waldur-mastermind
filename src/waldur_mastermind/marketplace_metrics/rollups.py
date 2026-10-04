"""Hourly and daily roll-ups of raw metric points.

Every run recomputes the buckets a point may still land in: anything since
the start of the day the late-data window reaches back to. Ingest refuses
points older than that window, so buckets before it never change again, and
recomputing instead of tracking what changed keeps corrections and
retries correct without any bookkeeping.
"""

import datetime

from constance import config
from django.db import connection
from django.utils import timezone

from . import enums, models, retention

ROLLUP_SQL = """
INSERT INTO {rollup} (
    series_id, granularity, bucket_start,
    count, sum, min, max, last, last_timestamp
)
SELECT
    series_id,
    %(granularity)s,
    date_trunc(%(granularity)s, timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
    count(*),
    sum(value),
    min(value),
    max(value),
    (array_agg(value ORDER BY timestamp DESC))[1],
    max(timestamp)
FROM {point}
WHERE timestamp >= %(since)s
GROUP BY series_id, 3
ON CONFLICT (series_id, granularity, bucket_start) DO UPDATE SET
    count = EXCLUDED.count,
    sum = EXCLUDED.sum,
    min = EXCLUDED.min,
    max = EXCLUDED.max,
    last = EXCLUDED.last,
    last_timestamp = EXCLUDED.last_timestamp
"""


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


def roll_up(since=None):
    since = since or recompute_since()
    sql = ROLLUP_SQL.format(
        rollup=models.MetricRollup._meta.db_table,
        point=models.MetricPoint._meta.db_table,
    )
    with connection.cursor() as cursor:
        for granularity in enums.Granularities.ROLLUPS:
            cursor.execute(sql, {"granularity": granularity, "since": since})
