"""Reports for the people who run calls, over one call or a whole call
managing organisation.

Every report takes a ``Scope``: the calls the reader may report on, narrowed
by the request's filters (calls, a round, a range of submission dates). The
scope is worked out once, by ``scope_for_organisation`` or ``scope_for_call``,
so no report has to repeat who may see which call.

The reports read what the calls already record; nothing is stored. They are
grouped aggregates, so their query count does not grow with the number of
calls, rounds, proposals or reviewers.

- ``portfolio``: per call and round, submissions, the outcome mix, the
  success rate and the mean review score; submissions per month.
- ``workflow``: per workflow step, the proposals waiting at it, overdue, and
  completed late, with median and 90th percentile days taken.
- ``reviewers``: per reviewer, invitations and their answers, reviews by
  state, overdue reviews, and how fast they answered and reviewed.
- ``rounds``: per round, the days between the steps of its lifecycle and how
  late its results were against its allocation date.
- ``budgets``: per resource pool, what it offers, what was awarded and
  remains, and what is requested by proposals still undecided.
"""

import dataclasses
import datetime
import uuid as uuid_lib
from collections import defaultdict
from decimal import Decimal

from django.db.models import (
    Avg,
    Case,
    Count,
    F,
    FloatField,
    Func,
    Max,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from rest_framework import exceptions

from waldur_core.core.models import User
from waldur_mastermind.proposal import budgets, models
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    CallStates,
    ProposalStates,
    ReviewRejectionCauses,
    RoundLifecycleStates,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.managers import assignment_item_status_q
from waldur_mastermind.proposal.workflow_observability import (
    SECONDS_PER_DAY,
    PercentileCont,
)

ReviewStates = models.Review.States


# -- scope -----------------------------------------------------------------------


@dataclasses.dataclass
class Scope:
    """The calls a report covers, and the filters on what it counts."""

    calls: "models.QuerySet[models.Call]"
    round: models.Round | None = None
    submitted_from: datetime.date | None = None
    submitted_to: datetime.date | None = None

    def submitted_q(self, prefix="") -> Q:
        """Proposals submitted within the dates, as a ``Q`` on ``prefix``
        (the path to the proposal). No condition when no date is given."""
        path = f"{prefix}__" if prefix else ""
        q = Q()
        if self.submitted_from:
            q &= Q(**{f"{path}submitted_at__gte": _day_start(self.submitted_from)})
        if self.submitted_to:
            q &= Q(
                **{
                    f"{path}submitted_at__lt": _day_start(
                        self.submitted_to + datetime.timedelta(days=1)
                    )
                }
            )
        return q

    def proposal_q(self, prefix="") -> Q:
        """The proposals the report counts: of the calls, of the round when
        one is given, submitted within the dates when they are."""
        path = f"{prefix}__" if prefix else ""
        q = Q(**{f"{path}round__call__in": self.calls}) & self.submitted_q(prefix)
        if self.round is not None:
            q &= Q(**{f"{path}round": self.round})
        return q


def _day_start(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def _parse_uuid(value):
    try:
        return uuid_lib.UUID(str(value)).hex
    except ValueError:
        raise exceptions.NotFound()


def organisation_calls(request, manager):
    """The calls of ``manager`` that the request may report on: every one for
    staff and support, else those whose configuration it may change -- as
    call manager of the call, or call organiser of the organisation, within
    a personal access token's scopes and bindings."""
    calls = models.Call.objects.filter(manager=manager)
    if request.user.is_staff or request.user.is_support:
        return calls
    return proposal_permissions.configurable_calls(request, calls)


def _narrowed(calls, call_uuids=None, round_uuid=None, submitted_from=None, to=None):
    """``calls`` narrowed by the filters. A call or round outside ``calls``
    is not found, rather than reported on as empty."""
    if call_uuids:
        wanted = {_parse_uuid(value) for value in call_uuids}
        calls = calls.filter(uuid__in=wanted)
        if calls.count() != len(wanted):
            raise exceptions.NotFound()
    call_round = None
    if round_uuid:
        call_round = models.Round.objects.filter(
            uuid=_parse_uuid(round_uuid), call__in=calls
        ).first()
        if call_round is None:
            raise exceptions.NotFound()
        calls = calls.filter(pk=call_round.call_id)
    return Scope(
        calls=calls,
        round=call_round,
        submitted_from=submitted_from,
        submitted_to=to,
    )


def scope_for_organisation(request, manager, filters) -> Scope:
    """The scope of an organisation-wide report. ``filters`` are the
    validated ``call_uuid``, ``round_uuid``, ``submitted_from`` and
    ``submitted_to``."""
    return _narrowed(
        organisation_calls(request, manager),
        filters.get("call_uuid"),
        filters.get("round_uuid"),
        filters.get("submitted_from"),
        filters.get("submitted_to"),
    )


def scope_for_all(filters) -> Scope:
    """The scope of a report over every call of every call managing
    organisation, for staff and support: narrowed to the organisations of
    ``organisation_uuid`` too, of which an unknown one is not found."""
    calls = models.Call.objects.all()
    organisation_uuids = filters.get("organisation_uuid")
    if organisation_uuids:
        wanted = {_parse_uuid(value) for value in organisation_uuids}
        organisations = models.CallManagingOrganisation.objects.filter(uuid__in=wanted)
        if organisations.count() != len(wanted):
            raise exceptions.NotFound()
        calls = calls.filter(manager__in=organisations)
    return _narrowed(
        calls,
        filters.get("call_uuid"),
        filters.get("round_uuid"),
        filters.get("submitted_from"),
        filters.get("submitted_to"),
    )


def scope_for_call(call, filters) -> Scope:
    """The scope of a report on ``call`` alone: the call filter does not
    apply."""
    return _narrowed(
        models.Call.objects.filter(pk=call.pk),
        None,
        filters.get("round_uuid"),
        filters.get("submitted_from"),
        filters.get("submitted_to"),
    )


# -- shared ----------------------------------------------------------------------


def _days(expression):
    """An interval expression in days."""
    return Func(
        expression,
        template=f"EXTRACT(EPOCH FROM %(expressions)s) / {SECONDS_PER_DAY}",
        output_field=FloatField(),
    )


def _rounded(value, digits=1):
    return round(value, digits) if value is not None else None


def _rate(part, whole):
    return round(part / whole, 3) if whole else None


def _span_days(start, end):
    """Days from ``start`` to ``end``; None unless both are known. Between
    two times, to the tenth of a day; when either is a date, in whole days
    between the two (local) dates."""
    if start is None or end is None:
        return None
    if isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime):
        return round((end - start).total_seconds() / SECONDS_PER_DAY, 1)
    return float((_local_day(end) - _local_day(start)).days)


def _local_day(value):
    if isinstance(value, datetime.datetime):
        return timezone.localdate(value)
    return value


def _call_row(call):
    """A call and its call managing organisation, as the rows naming a call
    give them. ``call`` comes with ``manager__customer`` selected."""
    return {
        "call_uuid": call.uuid.hex,
        "call_name": call.name,
        "call_slug": call.slug,
        "organisation_uuid": call.manager.uuid.hex,
        "organisation_name": call.manager.customer.name,
    }


# -- portfolio -------------------------------------------------------------------

OUTCOMES = {
    ProposalStates.DRAFT: "draft",
    ProposalStates.SUBMITTED: "awaiting_review",
    ProposalStates.IN_REVIEW: "in_review",
    ProposalStates.ACCEPTED: "accepted",
    ProposalStates.REJECTED: "rejected",
    ProposalStates.CANCELED: "canceled",
}


def _portfolio_block(counts, active_rounds):
    accepted = counts["accepted"]
    decided = accepted + counts["rejected"]
    scored = counts["scored_reviews"]
    return {
        "proposals": counts["proposals"],
        "submitted": counts["submitted"],
        **{name: counts[name] for name in OUTCOMES.values()},
        "decided": decided,
        "success_rate": _rate(accepted, decided),
        "scored_reviews": scored,
        "mean_score": _rounded(counts["score_sum"] / scored, 2) if scored else None,
        "active_rounds": active_rounds,
    }


def portfolio(scope: Scope, user=None) -> dict:
    """Per call and per round: proposals submitted, how they stand, the
    share of decided ones accepted, and the mean overall score of their
    submitted reviews; with the submissions per month."""
    calls = list(scope.calls.select_related("manager__customer").order_by("name", "id"))
    rounds = models.Round.objects.filter(call__in=scope.calls)
    if scope.round is not None:
        rounds = rounds.filter(pk=scope.round.pk)
    rounds = list(rounds.order_by("start_time", "id"))

    proposals = models.Proposal.objects.filter(scope.proposal_q())
    counts = defaultdict(lambda: defaultdict(int))
    for row in (
        proposals.values("round_id", "state")
        .annotate(
            n=Count("id"), submitted=Count("id", filter=Q(submitted_at__isnull=False))
        )
        .order_by()
    ):
        c = counts[row["round_id"]]
        c["proposals"] += row["n"]
        c["submitted"] += row["submitted"]
        c[OUTCOMES.get(row["state"], row["state"])] += row["n"]

    reviews = models.Review.objects.filter(
        scope.proposal_q("proposal"), state=ReviewStates.SUBMITTED, summary_score__gt=0
    )
    for row in (
        reviews.values("proposal__round_id")
        .annotate(n=Count("id"), total=Sum("summary_score"))
        .order_by()
    ):
        c = counts[row["proposal__round_id"]]
        c["scored_reviews"] += row["n"]
        c["score_sum"] += row["total"] or 0

    by_call = defaultdict(list)
    for call_round in rounds:
        by_call[call_round.call_id].append(call_round)

    def summed(round_list):
        total = defaultdict(int)
        for call_round in round_list:
            for key, value in counts[call_round.pk].items():
                total[key] += value
        return total

    # Open for submissions now: within its window, and of an active call, as
    # the organisation dashboard's count -- which also counts the rounds
    # scheduled to open, where this counts only those open.
    active_calls = {call.pk for call in calls if call.state == CallStates.ACTIVE}

    def is_active(call_round):
        return (
            call_round.call_id in active_calls
            and call_round.status == models.Round.Statuses.OPEN
        )

    call_rows = []
    for call in calls:
        call_rounds = by_call[call.pk]
        call_rows.append(
            {
                **_call_row(call),
                "call_state": call.state,
                **_portfolio_block(
                    summed(call_rounds), sum(map(is_active, call_rounds))
                ),
                "rounds": [
                    {
                        "round_uuid": call_round.uuid.hex,
                        "round_name": call_round.name,
                        "round_slug": call_round.slug,
                        "start_time": call_round.start_time,
                        "cutoff_time": call_round.cutoff_time,
                        **_portfolio_block(
                            counts[call_round.pk], int(is_active(call_round))
                        ),
                    }
                    for call_round in call_rounds
                ],
            }
        )

    months = [
        {"month": row["month"], "submitted": row["n"], "accepted": row["accepted"]}
        for row in proposals.filter(submitted_at__isnull=False)
        .annotate(month=TruncMonth("submitted_at"))
        .values("month")
        .annotate(
            n=Count("id"),
            accepted=Count("id", filter=Q(state=ProposalStates.ACCEPTED)),
        )
        .order_by("month")
    ]
    for month in months:
        if isinstance(month["month"], datetime.datetime):
            month["month"] = timezone.localdate(month["month"])

    return {
        "totals": _portfolio_block(summed(rounds), sum(map(is_active, rounds))),
        "calls": call_rows,
        "months": months,
    }


# -- workflow --------------------------------------------------------------------

_STEP_COUNTS = (
    "total",
    "pending",
    "waiting",
    "overdue",
    "completed",
    "completed_late",
    "skipped",
    "expired",
    "withdrawn",
)


# Evaluation first, then the phases that run on an accepted proposal.
_PHASE_ORDER = {
    phase: index for index, (phase, _label) in enumerate(WorkflowPhases.CHOICES)
}


def _step_figures(now):
    """The aggregates of ``workflow_observability.progress``, but outcomes."""
    status = WorkflowStepInstanceStatuses
    timed = Q(
        status=status.COMPLETED,
        started_at__isnull=False,
        completed_at__isnull=False,
    )
    taken = _days(F("completed_at") - F("started_at"))
    return {
        "total": Count("id"),
        "pending": Count("id", filter=Q(status=status.PENDING)),
        "waiting": Count("id", filter=Q(status=status.ACTIVE)),
        "overdue": Count("id", filter=Q(status=status.ACTIVE, deadline__lt=now)),
        "completed": Count("id", filter=Q(status=status.COMPLETED)),
        "completed_late": Count(
            "id",
            filter=Q(
                status=status.COMPLETED,
                deadline__isnull=False,
                completed_at__gt=F("deadline"),
            ),
        ),
        "skipped": Count("id", filter=Q(status=status.SKIPPED)),
        "expired": Count("id", filter=Q(status=status.EXPIRED)),
        "withdrawn": Count("id", filter=Q(status=status.WITHDRAWN)),
        "median_days": PercentileCont(taken, 0.5, filter=timed),
        "p90_days": PercentileCont(taken, 0.9, filter=timed),
    }


def workflow(scope: Scope, user=None, now=None) -> dict:
    """Per workflow step, by its key across the calls: the proposals waiting
    at it, overdue there, completed late, and how many days completing it
    took (median and 90th percentile); with each call's own figures.

    The figures ``workflow_observability.progress`` gives one call, over
    every call at once: one grouped query per call step and one per key,
    so a median across calls is the median of their step instances rather
    than a mean of medians. No outcome is counted, so no held decision can
    show through."""
    now = now or timezone.now()
    instances = models.ProposalWorkflowStepInstance.objects.filter(
        scope.proposal_q("proposal")
    )
    figures = _step_figures(now)
    by_step = {
        row["call_step_id"]: row
        for row in instances.values("call_step_id").annotate(**figures).order_by()
    }
    by_key = {
        row["call_step__key"]: row
        for row in instances.values("call_step__key").annotate(**figures).order_by()
    }

    steps = {}
    # A step's place among its call's steps of the same phase: calls differ
    # in which steps they have, so the merged steps are ordered by phase,
    # then by the earliest place a call gives the step within it.
    positions = defaultdict(int)
    for call_step in (
        models.CallWorkflowStep.objects.filter(call__in=scope.calls)
        .select_related("call__manager__customer")
        .order_by("order", "id")
    ):
        position = positions[call_step.call_id, call_step.phase]
        positions[call_step.call_id, call_step.phase] += 1
        row = by_step.get(call_step.pk)
        if row is None and not call_step.is_enabled:
            continue
        row = row or {}
        merged = by_key.get(call_step.key, {})
        step = steps.setdefault(
            call_step.key,
            {
                "key": call_step.key,
                "name": call_step.name,
                "phase": call_step.phase,
                "position": position,
                **{name: merged.get(name, 0) for name in _STEP_COUNTS},
                "median_days": _rounded(merged.get("median_days")),
                "p90_days": _rounded(merged.get("p90_days")),
                "calls": [],
            },
        )
        step["position"] = min(step["position"], position)
        step["calls"].append(
            {
                **_call_row(call_step.call),
                "name": call_step.name,
                "enabled": call_step.is_enabled,
                "planned_days": call_step.duration_in_days,
                **{name: row.get(name, 0) for name in _STEP_COUNTS},
                "median_days": _rounded(row.get("median_days")),
                "p90_days": _rounded(row.get("p90_days")),
            }
        )
    ordered = sorted(
        steps.values(),
        key=lambda step: (
            _PHASE_ORDER.get(step["phase"], len(_PHASE_ORDER)),
            step.pop("position"),
            step["key"],
        ),
    )
    return {
        "generated_at": now,
        "proposals": instances.values("proposal_id").distinct().count(),
        "steps": ordered,
    }


# -- reviewers -------------------------------------------------------------------

_POOL_ENTRY = "batch__reviewer_pool_entry"

# A review the reviewer has started on: scored, overall or by a criterion,
# or with a comment written.
_COMMENT_FIELDS = (
    "summary_public_comment",
    "summary_private_comment",
    "comment_project_title",
    "comment_project_summary",
    "comment_project_description",
    "comment_project_supporting_documentation",
    "comment_resource_requests",
    "comment_team",
)


def _started_q():
    q = Q(summary_score__gt=0) | ~Q(criteria_scores={})
    for field in _COMMENT_FIELDS:
        q |= Q(**{f"{field}__isnull": False}) & ~Q(**{field: ""})
    return q


_REVIEW_COUNTS = (
    "reviews_assigned",
    "reviews_submitted",
    "reviews_in_progress",
    "reviews_not_started",
    "reviews_declined",
    "reviews_expired",
    "reviews_withdrawn",
    "reviews_overdue",
)
# Every sent assignment item falls in exactly one of these, so they add up
# to ``invitations``.
_INVITATION_PARTS = (
    "invitations_accepted",
    "invitations_declined",
    "invitations_expired",
    "invitations_pending",
    "invitations_withdrawn",
    "invitations_coi_blocked",
    "invitations_reassigned",
)
_INVITATION_COUNTS = ("invitations", *_INVITATION_PARTS)


def _invitation_buckets():
    """A count per bucket of ``_INVITATION_PARTS``, on sent assignment items.

    By the item's effective status: a pending item whose batch is past its
    deadline has expired, whether or not the expiry task has stored that.
    One still pending in a batch the call team cancelled was withdrawn."""
    Statuses = AssignmentItemStatuses
    cancelled = Q(batch__status=AssignmentBatchStatuses.CANCELLED)
    waiting = Q(status=Statuses.PENDING)
    return {
        "invitations": Count("id"),
        "invitations_accepted": Count("id", filter=Q(status=Statuses.ACCEPTED)),
        "invitations_declined": Count("id", filter=Q(status=Statuses.DECLINED)),
        "invitations_expired": Count(
            "id",
            filter=assignment_item_status_q([Statuses.EXPIRED])
            & ~(waiting & cancelled),
        ),
        "invitations_pending": Count(
            "id", filter=assignment_item_status_q([Statuses.PENDING]) & ~cancelled
        ),
        "invitations_withdrawn": Count("id", filter=waiting & cancelled),
        "invitations_coi_blocked": Count("id", filter=Q(status=Statuses.COI_BLOCKED)),
        "invitations_reassigned": Count("id", filter=Q(status=Statuses.REASSIGNED)),
    }


def reviewers(scope: Scope, user=None, now=None) -> dict:
    """Per reviewer of the proposals counted: the assignments sent them and
    how they answered, their reviews by state, those overdue, and how fast
    they answered (from the batch being sent) and reviewed (from the review's
    creation to its submission), with the mean overall score they gave.

    Four grouped queries whatever the number of reviewers. Reviewers who
    have since been deactivated are listed too."""
    now = now or timezone.now()
    Causes = ReviewRejectionCauses
    in_review = Q(state=ReviewStates.IN_REVIEW)
    submitted = Q(state=ReviewStates.SUBMITTED)
    rejected = Q(state=ReviewStates.REJECTED)
    reviews = models.Review.objects.filter(scope.proposal_q("proposal"))
    figures = defaultdict(lambda: defaultdict(int))

    for row in (
        reviews.values("reviewer_id")
        .annotate(
            reviews_assigned=Count("id"),
            reviews_submitted=Count("id", filter=submitted),
            reviews_in_progress=Count("id", filter=in_review & _started_q()),
            reviews_not_started=Count("id", filter=in_review & ~_started_q()),
            # Rejected before the cause was recorded: declined, as
            # ``reviewer_stats`` counts it.
            reviews_declined=Count(
                "id",
                filter=rejected & Q(rejection_cause__in=[Causes.DECLINED, ""]),
            ),
            reviews_expired=Count(
                "id", filter=rejected & Q(rejection_cause=Causes.EXPIRED)
            ),
            reviews_withdrawn=Count(
                "id", filter=rejected & Q(rejection_cause=Causes.WITHDRAWN)
            ),
            mean_score=Avg("summary_score", filter=submitted & Q(summary_score__gt=0)),
            # Reviews submitted before the time was recorded have none, and
            # are left out.
            median_turnaround_days=PercentileCont(
                _days(F("submitted_at") - F("created")),
                0.5,
                filter=submitted & Q(submitted_at__isnull=False),
            ),
            last_submitted_at=Max("submitted_at", filter=submitted),
        )
        .order_by()
    ):
        figures[row.pop("reviewer_id")].update(row)

    for row in (
        reviews.with_deadline()
        .filter(in_review, review_deadline__lt=now)
        .values("reviewer_id")
        .annotate(n=Count("id", distinct=True))
        .order_by()
    ):
        figures[row["reviewer_id"]]["reviews_overdue"] = row["n"]

    # Invitations are the items of batches sent to the reviewer: a draft
    # batch's items have not reached anyone yet. A pool entry may be an
    # invitation by email not yet taken up, with no user behind it: its
    # items are listed under the address.
    items = (
        models.AssignmentItem.objects.filter(
            scope.proposal_q("proposal"), batch__sent_at__isnull=False
        )
        .annotate(
            assignee=Coalesce(
                f"{_POOL_ENTRY}__reviewer__user_id", f"{_POOL_ENTRY}__invited_user_id"
            )
        )
        .annotate(
            invitee=Case(
                When(assignee__isnull=True, then=F(f"{_POOL_ENTRY}__invited_email")),
                default=Value(""),
            )
        )
    )
    answered = Q(
        status__in=[AssignmentItemStatuses.ACCEPTED, AssignmentItemStatuses.DECLINED],
        responded_at__isnull=False,
    )
    by_address = defaultdict(lambda: defaultdict(int))
    for row in (
        items.values("assignee", "invitee")
        .annotate(
            **_invitation_buckets(),
            median_response_days=PercentileCont(
                _days(F("responded_at") - F("batch__sent_at")), 0.5, filter=answered
            ),
        )
        .order_by()
    ):
        user_id, address = row.pop("assignee"), row.pop("invitee")
        if user_id is not None:
            figures[user_id].update(row)
        else:
            for name in _INVITATION_COUNTS:
                by_address[address][name] += row[name]

    people = {
        person["id"]: person
        for person in User.all_objects.filter(pk__in=list(figures)).values(
            "id", "uuid", "first_name", "last_name", "email", "is_active"
        )
    }
    rows = []
    for user_id, row in figures.items():
        person = people.get(user_id)
        if person is None:
            continue
        rows.append(
            {
                "reviewer_uuid": person["uuid"].hex,
                "reviewer_name": f"{person['first_name']} {person['last_name']}".strip(),
                "reviewer_email": person["email"],
                "is_active": person["is_active"],
                **{name: row.get(name, 0) for name in _INVITATION_COUNTS},
                **{name: row.get(name, 0) for name in _REVIEW_COUNTS},
                "median_response_days": _rounded(row.get("median_response_days")),
                "median_turnaround_days": _rounded(row.get("median_turnaround_days")),
                "mean_score": _rounded(row.get("mean_score"), 2),
                "last_submitted_at": row.get("last_submitted_at"),
            }
        )
    for address, row in by_address.items():
        rows.append(
            {
                "reviewer_uuid": None,
                "reviewer_name": "",
                "reviewer_email": address,
                "is_active": None,
                **{name: row[name] for name in _INVITATION_COUNTS},
                **{name: 0 for name in _REVIEW_COUNTS},
                "median_response_days": None,
                "median_turnaround_days": None,
                "mean_score": None,
                "last_submitted_at": None,
            }
        )
    rows.sort(
        key=lambda r: (
            -r["reviews_assigned"],
            (r["reviewer_name"] or r["reviewer_email"]).lower(),
        )
    )
    totals = {
        name: sum(r[name] for r in rows)
        for name in (*_INVITATION_COUNTS, *_REVIEW_COUNTS)
    }
    return {"totals": {"reviewers": len(rows), **totals}, "reviewers": rows}


# -- rounds ----------------------------------------------------------------------


def rounds(scope: Scope, user=None, now=None) -> dict:
    """Per round, the days between the steps of its lifecycle -- cut-off,
    evaluation, deciding, results published, adopted -- and how late its
    results came against its allocation date. With dates, the rounds whose
    submission window overlaps them."""
    now = now or timezone.now()
    queryset = models.Round.objects.filter(call__in=scope.calls)
    if scope.round is not None:
        queryset = queryset.filter(pk=scope.round.pk)
    if scope.submitted_to:
        queryset = queryset.filter(
            start_time__lt=_day_start(scope.submitted_to + datetime.timedelta(days=1))
        )
    if scope.submitted_from:
        queryset = queryset.filter(
            Q(cutoff_time__isnull=True)
            | Q(cutoff_time__gte=_day_start(scope.submitted_from))
        )
    queryset = (
        queryset.select_related("call__manager__customer")
        .annotate(
            # Within the dates as well, as the portfolio counts them.
            proposals_submitted=Count(
                "proposal",
                filter=Q(proposal__submitted_at__isnull=False)
                & scope.submitted_q("proposal"),
            )
        )
        .order_by("start_time", "id")
    )
    rows = []
    today = timezone.localdate(now)
    for call_round in queryset:
        published = call_round.results_published_at
        allocation = call_round.allocation_date
        # The allocation date is a day: lateness is counted in whole days.
        if allocation is None:
            delay = None
        elif published is not None:
            delay = (_local_day(published) - _local_day(allocation)).days
        elif (
            _local_day(allocation) < today
            and call_round.lifecycle_state in _AWAITING_RESULTS
            and call_round.call.state not in _CALL_NOT_ACTIVE
        ):
            # Only a round still to publish its results is running late: not
            # one of a call not active, nor one closed without publishing,
            # as rounds from before the lifecycle was recorded were.
            delay = (today - _local_day(allocation)).days
        else:
            delay = None
        stage, stage_started = _current_stage(call_round, now)
        rows.append(
            {
                **_call_row(call_round.call),
                "round_uuid": call_round.uuid.hex,
                "round_name": call_round.name,
                "round_slug": call_round.slug,
                "call_state": call_round.call.state,
                "status": _CALL_NOT_ACTIVE.get(
                    call_round.call.state, call_round.status
                ),
                "lifecycle_state": call_round.lifecycle_state,
                "proposals_submitted": call_round.proposals_submitted,
                "start_time": call_round.start_time,
                "cutoff_time": call_round.cutoff_time,
                "evaluation_started_at": call_round.evaluation_started_at,
                "deciding_started_at": call_round.deciding_started_at,
                "results_published_at": published,
                "adopted_at": call_round.adopted_at,
                "closed_at": call_round.closed_at,
                "allocation_date": allocation,
                "days_cutoff_to_evaluation": _span_days(
                    call_round.cutoff_time, call_round.evaluation_started_at
                ),
                "days_evaluating": _span_days(
                    call_round.evaluation_started_at, call_round.deciding_started_at
                ),
                "days_deciding": _span_days(call_round.deciding_started_at, published),
                "days_published_to_adopted": _span_days(
                    published, call_round.adopted_at
                ),
                "days_cutoff_to_published": _span_days(
                    call_round.cutoff_time, published
                ),
                "days_after_allocation_date": delay,
                "late": delay is not None and delay > 0,
                "current_stage": stage,
                "days_in_current_stage": _span_days(stage_started, now),
            }
        )
    cycles = sorted(
        row["days_cutoff_to_published"]
        for row in rows
        if row["days_cutoff_to_published"] is not None
    )
    return {
        "totals": {
            "rounds": len(rows),
            "published": len(cycles),
            "late": sum(row["late"] for row in rows),
            "median_days_cutoff_to_published": _median(cycles),
        },
        "rounds": rows,
    }


# A round of a call not active takes no submissions, whatever its times
# say: of an archived call, stopped; of a draft call, not started.
# Where a round stands while its results are still to be published.
_AWAITING_RESULTS = (
    None,
    RoundLifecycleStates.EVALUATING,
    RoundLifecycleStates.DECIDING,
)
_CALL_NOT_ACTIVE = {
    CallStates.ARCHIVED: "call_archived",
    CallStates.DRAFT: "call_draft",
}


def _current_stage(call_round, now):
    """The stage a round not closed yet is at, and since when: open for
    submissions, past its cut-off before its evaluation started, evaluating,
    deciding, or with its results published. ``(None, None)`` for a closed
    round, and no start for one not open yet. A round of an archived or a
    draft call is at no stage, whatever its times say."""
    States = RoundLifecycleStates
    state = call_round.lifecycle_state
    if state == States.CLOSED:
        return None, None
    if call_round.call.state in _CALL_NOT_ACTIVE:
        return _CALL_NOT_ACTIVE[call_round.call.state], None
    if state == States.RESULTS_PUBLISHED:
        return state, call_round.results_published_at
    if state == States.DECIDING:
        return state, call_round.deciding_started_at
    if state == States.EVALUATING:
        return state, call_round.evaluation_started_at
    status = call_round.status
    if status == models.Round.Statuses.SCHEDULED:
        return status, None
    if status == models.Round.Statuses.OPEN:
        return status, call_round.start_time
    return "awaiting_evaluation", call_round.cutoff_time


def _median(values):
    """The median of sorted ``values``; None of none."""
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return round((values[middle - 1] + values[middle]) / 2, 1)


# -- budgets ---------------------------------------------------------------------


def budgets_usage(scope: Scope, user=None) -> dict:
    """Per resource pool of the calls -- and per component awarded or asked
    for with no pool -- what it offers, what was requested and awarded,
    what remains, and what proposals still waiting for their decision ask
    of it. Awards whose decision is held from ``user`` are not counted.

    Each row is a budget of its own: a call-wide row only for a call-wide
    pool, and then a round row only for a pool of the round's own, marked
    ``within_call_pool`` because its awards count against both. Leaving
    those out, the rows add up without counting anything twice. A round
    filter keeps the call-wide pools, whose figures stay the whole call's:
    the round draws on them too.

    ``budgets.calls_usage`` over every call at once, and one query for
    every call's undecided requests, whatever the number of calls. The submission dates do not apply: a pool is offered per call
    and round."""
    held_from = (
        proposal_permissions.proposals_with_held_decisions_hidden_from(user)
        if user is not None
        else None
    )
    # Over every round, also with a round filter: a call-wide pool's row
    # counts what all its rounds ask of it.
    undecided = budgets.undecided_amounts(
        budgets.undecided_items(held_from).filter(proposal__round__call__in=scope.calls)
    )
    zero = Decimal(0)
    rows = []
    usage = budgets.calls_usage(scope.calls, held_from=held_from)
    for call in scope.calls.select_related("manager__customer").order_by("name", "id"):
        for component in usage[call.pk]:
            key = (component["requested_offering"].pk, component["component_type"])
            call_cell, *round_cells = component["cells"]
            # The call-wide cell sums the rounds' amounts: a row of its own
            # only for a call-wide pool, which the round rows then draw on.
            # With a round filter too: the call's pool is that round's budget.
            has_call_pool = call_cell["pool"] is not None
            cells = [call_cell] if has_call_pool else []
            for cell in round_cells:
                if scope.round is None or cell["round"] == scope.round:
                    cells.append(cell)
            for cell in cells:
                call_round = cell["round"]
                round_id = call_round.pk if call_round else None
                waiting = (
                    undecided[key].get(round_id, zero) if key in undecided else zero
                )
                pool = cell["pool"]
                if pool is None and (
                    has_call_pool
                    or not (cell["requested"] or cell["awarded"] or waiting)
                ):
                    continue
                offered = cell["offered_total"]
                rows.append(
                    {
                        **_call_row(call),
                        "round_uuid": call_round.uuid.hex if call_round else None,
                        "round_name": call_round.name if call_round else None,
                        "pool_uuid": pool.uuid.hex if pool else None,
                        "within_call_pool": call_round is not None and has_call_pool,
                        "offering_name": component["offering_name"],
                        "component_type": component["component_type"],
                        "component_name": component["component_name"],
                        "measured_unit": component["measured_unit"],
                        "limit_period": component["limit_period"],
                        "offered_total": offered,
                        "requested": cell["requested"],
                        "awarded": cell["awarded"],
                        "remaining": cell["remaining"],
                        "undecided": waiting,
                        "awarded_share": (
                            round(float(cell["awarded"] / offered), 3)
                            if offered
                            else None
                        ),
                    }
                )
    return {
        "totals": {
            "pools": sum(row["pool_uuid"] is not None for row in rows),
            "pools_exhausted": sum(
                row["remaining"] is not None and row["remaining"] <= 0 for row in rows
            ),
        },
        "pools": rows,
    }
