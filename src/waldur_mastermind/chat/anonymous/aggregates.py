"""Pure aggregate helpers for the staff KPI endpoint.

Kept separate from views so each function can be unit-tested without
HTTP plumbing and reused by other admin surfaces if needed.
"""

import datetime as dt
from collections import OrderedDict

from django.db.models import Count, Max, Q
from django.utils import timezone

from waldur_mastermind.chat.input_guards.base import SeverityLevel

SEVERITIES = tuple(s.name for s in SeverityLevel)


def _date_labels(days: int):
    today = timezone.now().date()
    return [today - dt.timedelta(days=i) for i in range(days - 1, -1, -1)]


def daily_volume(qs, days: int):
    """Return [{date: 'YYYY-MM-DD', count: int}, ...] padded across the window."""
    labels = _date_labels(days)
    counts = dict(
        qs.filter(created__date__gte=labels[0])
        .values_list("created__date")
        .annotate(c=Count("uuid"))
    )
    return [{"date": d.isoformat(), "count": counts.get(d, 0)} for d in labels]


def severity_by_day(qs, days: int = 14):
    """Return {labels: [date], series: {NONE: [...], LOW: [...], ...}}."""
    labels = _date_labels(days)
    label_index = {d: i for i, d in enumerate(labels)}
    series = OrderedDict((s, [0] * days) for s in SEVERITIES)
    rows = (
        qs.filter(created__date__gte=labels[0])
        .values("created__date", "severity")
        .annotate(c=Count("uuid"))
    )
    for row in rows:
        idx = label_index.get(row["created__date"])
        if idx is None:
            continue
        # Keys uppercased for frontend chart legend constants — DB stores lowercase.
        sev = (row["severity"] or "none").upper()
        if sev not in series:
            continue
        series[sev][idx] = row["c"]
    return {
        "labels": [d.isoformat() for d in labels],
        "series": series,
    }


def user_aggregates(qs):
    """Per-user-slug aggregate rows powering the staff Users list.

    Raw IPs are intentionally not surfaced here — staff should identify actors
    by ``user_slug`` (the stable pseudonym), not by IP. Per-interaction IPs are
    still available via the transcript endpoint for forensic lookups.
    """
    grouped = (
        qs.exclude(user_slug="")
        .values("user_slug")
        .annotate(
            total_interactions=Count("uuid"),
            session_count=Count("session_id", distinct=True),
            positive_feedback=Count("uuid", filter=Q(feedback__score=1)),
            negative_feedback=Count("uuid", filter=Q(feedback__score=-1)),
            injection_strikes=Count(
                "uuid", filter=Q(action_taken__in=("block", "redact"))
            ),
            last_seen=Max("created"),
        )
        .order_by("-total_interactions")
    )
    enriched = []
    for row in grouped:
        # Counts rows without a thumbs verdict — feedback may exist with score=None (LLM-only review).
        no_feedback = (
            row["total_interactions"]
            - row["positive_feedback"]
            - row["negative_feedback"]
        )
        enriched.append(
            {
                **row,
                "no_feedback": no_feedback,
                "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            }
        )
    return enriched
