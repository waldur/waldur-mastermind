"""Pure aggregate helpers for the staff KPI endpoint.

Kept separate from views so each function can be unit-tested without
HTTP plumbing and reused by other admin surfaces if needed.
"""

import datetime as dt
from collections import OrderedDict

from django.db.models import (
    Case,
    Count,
    IntegerField,
    Max,
    Min,
    Q,
    StringAgg,
    Sum,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from waldur_mastermind.chat.anonymous import models
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


# Ordinal for reducing a conversation's per-interaction severities to the single
# highest level; kept here so the SQL rank and the label lookup can't drift.
_SEVERITY_RANK = {
    SeverityLevel.CRITICAL.value: 4,
    SeverityLevel.HIGH.value: 3,
    SeverityLevel.MEDIUM.value: 2,
    SeverityLevel.LOW.value: 1,
}
_RANK_TO_SEVERITY = {rank: value for value, rank in _SEVERITY_RANK.items()}


def conversation_queryset(qs):
    """The grouped conversation rows, before the click join and row build.

    Split out so callers can narrow on the aggregates themselves: filtering
    this queryset lands in HAVING, which selects whole conversations. The
    same predicate applied to ``qs`` would drop individual turns and
    re-aggregate the survivors, quietly rewriting every other column on a
    partially-matching conversation.
    """
    return (
        qs.exclude(session_id="")
        .values("session_id")
        .annotate(
            message_count=Count("uuid"),
            user_slug=Max("user_slug"),
            flagged_count=Count("uuid", filter=Q(is_flagged=True)),
            positive_feedback=Count("uuid", filter=Q(feedback__score=1)),
            negative_feedback=Count("uuid", filter=Q(feedback__score=-1)),
            # Same 1:1 feedback join as the two counts above, so this adds no
            # row multiplication.
            reviewed_count=Count(
                "uuid", filter=Q(feedback__llm_reviewed_at__isnull=False)
            ),
            offerings_shown=Sum("result_count"),
            severity_rank=Max(
                Case(
                    *[
                        When(severity=level, then=Value(rank))
                        for level, rank in _SEVERITY_RANK.items()
                    ],
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ),
            # Safe in this annotate(): model is a column on the interaction
            # row, so unlike clicks it adds no join and cannot multiply rows.
            models_used=Coalesce(
                StringAgg("model", Value(", "), distinct=True, filter=~Q(model="")),
                Value(""),
                output_field=TextField(),
            ),
            # Suffixed rather than named after the public field: a bare
            # ``input_tokens`` alias shadows the concrete column, and a later
            # .filter() on that name can resolve to the column and emit WHERE
            # instead of HAVING — turn-level narrowing wearing a
            # conversation-level name.
            input_tokens_sum=Coalesce(Sum("input_tokens"), Value(0)),
            output_tokens_sum=Coalesce(Sum("output_tokens"), Value(0)),
            total_tokens=Coalesce(Sum("input_tokens"), Value(0))
            + Coalesce(Sum("output_tokens"), Value(0)),
            started=Min("created"),
            last_active=Max("created"),
        )
        .order_by("-last_active")
    )


def session_aggregates(qs, grouped=None):
    """Per-session aggregate rows — one row per anonymous conversation.

    Mirrors the authenticated ThreadSession table: a conversation is a
    ``session_id`` group. Severity collapses to the highest level seen in the
    conversation, and feedback/flagged collapse to booleans.

    ``grouped`` accepts an already-narrowed :func:`conversation_queryset` so
    the caller can apply HAVING-level filters; ``qs`` is still needed for the
    click join, which counts against the unfiltered interaction rows.
    """
    if grouped is None:
        grouped = conversation_queryset(qs)
    # Counted in its own query rather than as another annotate(): clicks is a
    # non-unique FK, so joining it here would multiply the interaction rows and
    # silently inflate message_count and offerings_shown.
    click_counts = dict(
        models.AnonymousChatClick.objects.filter(interaction__in=qs)
        .values("interaction__session_id")
        .annotate(clicked=Count("id"))
        .values_list("interaction__session_id", "clicked")
    )

    rows = []
    for row in grouped:
        rows.append(
            {
                "session_id": row["session_id"],
                "user_slug": row["user_slug"],
                "message_count": row["message_count"],
                "is_flagged": row["flagged_count"] > 0,
                "max_severity": _RANK_TO_SEVERITY.get(
                    row["severity_rank"], SeverityLevel.NONE.value
                ),
                "has_feedback": (row["positive_feedback"] + row["negative_feedback"])
                > 0,
                "offerings_shown": row["offerings_shown"] or 0,
                "offerings_clicked": click_counts.get(row["session_id"], 0),
                "models_used": row["models_used"],
                # Same shape as is_flagged above: the count stays an internal
                # annotation so the conversation filterset can use it in
                # HAVING, while the row carries the fact callers actually want.
                "is_reviewed": row["reviewed_count"] > 0,
                "input_tokens": row["input_tokens_sum"],
                "output_tokens": row["output_tokens_sum"],
                "total_tokens": row["total_tokens"],
                "started": row["started"],
                "last_active": row["last_active"],
            }
        )
    return rows
