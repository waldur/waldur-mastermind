"""A reviewer's record, worked out from their reviews and assignments.

Nothing is stored: every figure is read from the reviews and assignment items
as they stand when asked for, so none can fall behind them.

- completed: reviews submitted;
- declined: assignments the reviewer declined, and reviews they declined or
  that were rejected before the cause was recorded;
- timed out: assignments left unanswered until they expired, and reviews
  rejected at their deadline;
- average score: the mean overall score of the submitted reviews;
- average review time and last review: from a submitted review's creation to
  its submission -- its last change, for reviews submitted before the
  submission time was recorded.

A review withdrawn because the reviewer joined the proposal team counts as
none of these.
"""

from django.contrib.auth import get_user_model
from django.db.models import (
    Avg,
    Count,
    DateTimeField,
    DurationField,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    Max,
    OuterRef,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from waldur_mastermind.proposal import enums, models

Causes = enums.ReviewRejectionCauses
ItemStatuses = enums.AssignmentItemStatuses
States = models.Review.States

_REVIEWER = "reviewer"
_ASSIGNEE = "batch__reviewer_pool_entry__reviewer__user"


def _per_user(queryset, user_field, user_ref, value, output_field):
    """``value`` aggregated over the rows of ``queryset`` that belong to the
    outer row's user."""
    return Subquery(
        queryset.filter(**{user_field: OuterRef(user_ref)})
        .order_by()
        .values(user_field)
        .annotate(value=value)
        .values("value")[:1],
        output_field=output_field,
    )


def _count(queryset, user_field, user_ref):
    return Coalesce(
        _per_user(queryset, user_field, user_ref, Count("id"), IntegerField()),
        Value(0),
    )


def _submitted_at():
    """When a submitted review was submitted: its last change, for one
    submitted before that was recorded."""
    return Coalesce(F("submitted_at"), F("modified"), output_field=DateTimeField())


def annotate(queryset, user_ref="user_id"):
    """``queryset`` with each row's figures as ``stats_*`` annotations.

    ``user_ref`` names the field of the row that holds the reviewer's user:
    ``user_id`` for reviewer profiles, ``pk`` for users. One subquery per
    figure, whatever the number of rows.
    """
    reviews = models.Review.objects.all()
    submitted = reviews.filter(state=States.SUBMITTED)
    rejected = reviews.filter(state=States.REJECTED)
    items = models.AssignmentItem.objects.all()

    return queryset.annotate(
        stats_completed=_count(submitted, _REVIEWER, user_ref),
        stats_declined=_count(
            rejected.exclude(rejection_cause__in=[Causes.EXPIRED, Causes.WITHDRAWN]),
            _REVIEWER,
            user_ref,
        )
        + _count(items.filter(status=ItemStatuses.DECLINED), _ASSIGNEE, user_ref),
        stats_timed_out=_count(
            rejected.filter(rejection_cause=Causes.EXPIRED), _REVIEWER, user_ref
        )
        + _count(items.filter(status=ItemStatuses.EXPIRED), _ASSIGNEE, user_ref),
        stats_score=_per_user(
            submitted.filter(summary_score__gt=0),
            _REVIEWER,
            user_ref,
            Avg("summary_score"),
            FloatField(),
        ),
        stats_review_time=_per_user(
            submitted,
            _REVIEWER,
            user_ref,
            Avg(
                ExpressionWrapper(
                    _submitted_at() - F("created"), output_field=DurationField()
                )
            ),
            DurationField(),
        ),
        stats_last_review=_per_user(
            submitted, _REVIEWER, user_ref, Max(_submitted_at()), DateTimeField()
        ),
    )


def _figures(row) -> dict:
    review_time = row.stats_review_time
    score = row.stats_score
    last_review = row.stats_last_review
    return {
        "total_reviews_completed": row.stats_completed,
        "total_reviews_declined": row.stats_declined,
        "total_reviews_timeout": row.stats_timed_out,
        "average_review_time_days": (
            round(review_time.total_seconds() / 86400, 2)
            if review_time is not None
            else None
        ),
        "average_score_given": round(score, 2) if score is not None else None,
        "last_review_date": (
            timezone.localdate(last_review) if last_review is not None else None
        ),
    }


def of_profile(profile: models.ReviewerProfile) -> dict:
    """The figures of the reviewer behind ``profile``, read from its
    annotations when it came from ``annotate``."""
    if not hasattr(profile, "stats_completed"):
        profile = annotate(models.ReviewerProfile.objects.filter(pk=profile.pk)).get()
    return _figures(profile)


def of_user(user) -> dict:
    """The figures of ``user`` as a reviewer, whether or not they have a
    reviewer profile."""
    return _figures(
        annotate(get_user_model().objects.filter(pk=user.pk), user_ref="pk").get()
    )
