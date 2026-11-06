"""How a running call's proposals move through its workflow.

Three readings for the call team of a pilot call, from what the workflow
already records -- no table of its own:

- ``progress``: per step, how many proposals wait at it now and how many of
  those are overdue, how long completed ones took against the planned
  duration (median and 90th percentile), how many were skipped, expired or
  withdrawn, and the share of each outcome. Aggregate queries over the step
  instances, so the number of queries does not grow with the proposals.
- ``proposal_timeline``: one proposal's steps, planned against actual, who
  completed each, and the workflow mail sent about it -- the audience as a
  role and a count, never an address.
- ``paths``: the distinct sequences of (step, outcome) the proposals took,
  with how many took each and whether a saved scenario covers it.

Who may read them is the views' to decide: the call team, staff and support.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence

from django.contrib.contenttypes.models import ContentType
from django.db.models import Aggregate, Count, F, FloatField, Func, Q
from django.utils import timezone

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import WorkflowStepInstanceStatuses

SECONDS_PER_DAY = 86400.0


class PercentileCont(Aggregate):
    """Postgres' continuous percentile of an ordered set."""

    function = "PERCENTILE_CONT"
    name = "PercentileCont"
    template = (
        "%(function)s(%(percentile)s) WITHIN GROUP (ORDER BY %(expressions)s)%(filter)s"
    )
    output_field = FloatField()

    def __init__(self, expression, percentile, **extra):
        percentile = float(percentile)
        if not 0 <= percentile <= 1:
            raise ValueError("The percentile is a fraction between 0 and 1.")
        super().__init__(expression, percentile=percentile, **extra)


def _days_taken():
    """Days from a step instance's start to its completion."""
    return Func(
        F("completed_at") - F("started_at"),
        template=f"EXTRACT(EPOCH FROM %(expressions)s) / {SECONDS_PER_DAY}",
        output_field=FloatField(),
    )


def _rounded(value):
    return round(value, 1) if value is not None else None


def _instances(call, call_round=None):
    queryset = models.ProposalWorkflowStepInstance.objects.filter(
        proposal__round__call=call
    )
    if call_round is not None:
        queryset = queryset.filter(proposal__round=call_round)
    return queryset


def _rounds(call):
    return [
        {
            "uuid": r.uuid.hex,
            "name": r.name,
            "start_time": r.start_time,
            "cutoff_time": r.cutoff_time,
            "allocation_date": r.allocation_date,
        }
        for r in call.round_set.order_by("start_time", "id")
    ]


def _held_from(user):
    """The allocation decisions ``user`` may not learn the outcome of: those
    held for the round's publication, of proposals the round still withholds
    from them -- their own, when they also manage the call. None for no
    one's."""
    if user is None:
        return None
    hidden = proposal_permissions.proposals_with_held_decisions_hidden_from(user)
    if hidden is None:
        return None
    return Q(
        is_allocation_decision=True,
        proposal__decision_held=True,
        proposal__in=hidden,
    )


# -- progress ------------------------------------------------------------------


def progress(call, call_round=None, now=None, user=None) -> dict:
    """Per step of ``call``, where its proposals are and how long steps took,
    over every round or ``call_round`` alone. The outcomes of decisions held
    from ``user`` are not counted."""
    now = now or timezone.now()
    instances = _instances(call, call_round)
    held = _held_from(user)
    status = WorkflowStepInstanceStatuses
    timed = Q(
        status=status.COMPLETED,
        started_at__isnull=False,
        completed_at__isnull=False,
    )
    rows = {
        row["call_step_id"]: row
        for row in instances.values("call_step_id")
        .annotate(
            total=Count("id"),
            pending=Count("id", filter=Q(status=status.PENDING)),
            waiting=Count("id", filter=Q(status=status.ACTIVE)),
            overdue=Count("id", filter=Q(status=status.ACTIVE, deadline__lt=now)),
            completed=Count("id", filter=Q(status=status.COMPLETED)),
            completed_late=Count(
                "id",
                filter=Q(
                    status=status.COMPLETED,
                    deadline__isnull=False,
                    completed_at__gt=F("deadline"),
                ),
            ),
            skipped=Count("id", filter=Q(status=status.SKIPPED)),
            expired=Count("id", filter=Q(status=status.EXPIRED)),
            withdrawn=Count("id", filter=Q(status=status.WITHDRAWN)),
            median_days=PercentileCont(_days_taken(), 0.5, filter=timed),
            p90_days=PercentileCont(_days_taken(), 0.9, filter=timed),
        )
        .order_by()
    }
    outcomes = defaultdict(list)
    decided = instances.filter(status=status.COMPLETED).exclude(outcome="")
    if held is not None:
        decided = decided.exclude(held)
    for row in (
        decided.values("call_step_id", "outcome")
        .annotate(count=Count("id"))
        .order_by("call_step_id", "-count", "outcome")
    ):
        outcomes[row["call_step_id"]].append(row)
    proposals = instances.values("proposal_id").distinct().count()
    per_round = _round_step_progress(call, call_round, instances, now)

    steps = []
    for step in models.CallWorkflowStep.objects.filter(call=call).order_by(
        "order", "id"
    ):
        row = rows.get(step.pk, {})
        completed = row.get("completed", 0)
        steps.append(
            {
                "key": step.key,
                "name": step.name,
                "enabled": step.is_enabled,
                "phase": step.phase,
                "round_scoped": step.round_scoped,
                # A step run once per round is counted per round as well:
                # its proposals move on together.
                "rounds": per_round.get(step.pk, []),
                "planned_days": step.duration_in_days,
                "total": row.get("total", 0),
                "pending": row.get("pending", 0),
                "waiting": row.get("waiting", 0),
                "overdue": row.get("overdue", 0),
                "completed": completed,
                "completed_late": row.get("completed_late", 0),
                "skipped": row.get("skipped", 0),
                "expired": row.get("expired", 0),
                "withdrawn": row.get("withdrawn", 0),
                "median_days": _rounded(row.get("median_days")),
                "p90_days": _rounded(row.get("p90_days")),
                "outcomes": [
                    {
                        "outcome": o["outcome"],
                        "count": o["count"],
                        "share": round(o["count"] / completed, 3) if completed else 0,
                    }
                    for o in outcomes.get(step.pk, [])
                ],
            }
        )
    return {
        "round": call_round.uuid.hex if call_round is not None else None,
        "rounds": _rounds(call),
        "generated_at": now,
        "proposals": proposals,
        "steps": steps,
    }


def _round_step_progress(call, call_round, instances, now):
    """Per step run once per round, one entry per round: the proposals at it
    now, whether it was opened, when it is due, and its round reports
    submitted. Aggregates, so the queries do not grow with the rounds."""
    status = WorkflowStepInstanceStatuses
    round_steps = models.CallWorkflowStep.objects.filter(call=call, round_scoped=True)
    if not round_steps.exists():
        return {}
    rounds = call.round_set.order_by("start_time", "id")
    if call_round is not None:
        rounds = rounds.filter(pk=call_round.pk)
    rounds = list(rounds)
    at_step = {
        (row["call_step_id"], row["proposal__round_id"]): row
        for row in instances.filter(call_step__round_scoped=True)
        .values("call_step_id", "proposal__round_id")
        .annotate(
            waiting=Count("id", filter=Q(status=status.ACTIVE)),
            completed=Count("id", filter=Q(status=status.COMPLETED)),
        )
        .order_by()
    }
    opened = {
        (row.call_step_id, row.round_id): row
        for row in models.RoundStep.objects.filter(
            call_step__in=round_steps, round__in=rounds
        )
    }
    reports = {
        (row["call_step_id"], row["round_id"]): row
        for row in models.RoundMeetingNote.objects.filter(
            call_step__in=round_steps, round__in=rounds
        )
        .values("call_step_id", "round_id")
        .annotate(
            total=Count("id"),
            submitted=Count("id", filter=Q(submitted_at__isnull=False)),
        )
        .order_by()
    }
    result = {}
    for step in round_steps:
        entries = []
        for call_round_ in rounds:
            key = (step.pk, call_round_.pk)
            row = at_step.get(key, {})
            round_step = opened.get(key)
            due_at = round_step.due_at if round_step else None
            entries.append(
                {
                    "round": call_round_.uuid.hex,
                    "name": call_round_.name,
                    "opened": bool(round_step and round_step.opened_at),
                    "due_at": due_at,
                    "overdue": bool(due_at and due_at < now and row.get("waiting", 0)),
                    "waiting": row.get("waiting", 0),
                    "completed": row.get("completed", 0),
                    "reports": reports.get(key, {}).get("total", 0),
                    "reports_submitted": reports.get(key, {}).get("submitted", 0),
                }
            )
        result[step.pk] = entries
    return result


# -- one proposal ----------------------------------------------------------------


def _person(user):
    if user is None:
        return None
    return user.full_name or user.username


def notification_events(proposal):
    """The notification-sent events logged on ``proposal``, oldest first."""
    return (
        logging_models.Event.objects.filter(
            event_type=EventType.PROPOSAL_WORKFLOW_NOTIFICATION_SENT,
            feed__content_type=ContentType.objects.get_for_model(models.Proposal),
            feed__object_id=proposal.pk,
        )
        .order_by("created", "id")
        .distinct()
    )


def proposal_timeline(proposal, user, now=None) -> dict:
    """``proposal``'s steps planned against actual, and the mail sent about
    it, for a reader on the call team."""
    now = now or timezone.now()
    held = proposal.decision_held and not (
        proposal_permissions.user_can_view_held_decision(user, proposal)
    )
    steps = []
    for instance in proposal.workflow_step_instances.select_related(
        "call_step", "completed_by"
    ).order_by("order", "id"):
        call_step = instance.call_step
        outcome = instance.outcome or None
        if held and instance.is_allocation_decision:
            outcome = None
        overdue = bool(
            instance.deadline
            and (
                (
                    instance.status == WorkflowStepInstanceStatuses.ACTIVE
                    and instance.deadline < now
                )
                or (instance.completed_at and instance.completed_at > instance.deadline)
            )
        )
        steps.append(
            {
                "key": call_step.key,
                "name": call_step.name or instance.step,
                "stage": instance.stage,
                "status": instance.status,
                "outcome": outcome,
                "planned_days": call_step.duration_in_days,
                "started_at": instance.started_at,
                "completed_at": instance.completed_at,
                "deadline": instance.deadline,
                "overdue": overdue,
                "completed_by_name": _person(instance.completed_by),
                # Mail recorded before the sends were logged: what went out,
                # with no time.
                "sent_notifications": list(instance.sent_notifications or []),
            }
        )
    notifications = [
        {
            "created": event.created,
            "step_key": event.context.get("step_key"),
            "step_name": event.context.get("step_name"),
            "template": event.context.get("template", ""),
            "trigger": event.context.get("trigger", ""),
            "audience": event.context.get("audience", ""),
            "recipient_count": event.context.get("recipient_count", 0),
            "email_log_uuids": list(event.context.get("email_log_uuids") or []),
        }
        for event in notification_events(proposal)
    ]
    return {
        "proposal_uuid": proposal.uuid.hex,
        "proposal_name": proposal.name,
        "state": proposal.state,
        "generated_at": now,
        "steps": steps,
        "notifications": notifications,
    }


# -- paths ---------------------------------------------------------------------

# Functions ``call -> iterable of paths``, each a sequence of ``(step key,
# outcome)``, giving the paths a call's saved scenarios walk. None from every
# source, or no source at all, leaves coverage unknown.
SCENARIO_PATH_SOURCES: list[Callable] = []


def scenario_paths(call) -> list[tuple] | None:
    """The paths ``call``'s saved scenarios walk; None when nothing says."""
    found = None
    for source in SCENARIO_PATH_SOURCES:
        paths = source(call)
        if paths is None:
            continue
        found = (found or []) + [tuple(tuple(p) for p in path) for path in paths]
    return found


def _covered(path: Sequence, scenarios: Iterable[tuple]) -> bool:
    """Whether a scenario walks ``path``: the whole of it, or for a path
    still under way, its beginning."""
    path = tuple(path)
    return any(scenario[: len(path)] == path for scenario in scenarios)


def paths(call, call_round=None, user=None) -> dict:
    """The distinct paths ``call``'s proposals took through its workflow. A
    decision held from ``user`` shows as completed, without its outcome."""
    status = WorkflowStepInstanceStatuses
    by_proposal = defaultdict(list)
    open_proposals = set()
    instances = _instances(call, call_round).exclude(status=status.PENDING)
    held = _held_from(user)
    masked = (
        set(instances.filter(held).values_list("id", flat=True))
        if held is not None
        else set()
    )
    for instance_id, proposal_id, key, step_status, outcome in instances.order_by(
        "proposal_id", "order", "id"
    ).values_list("id", "proposal_id", "call_step__key", "status", "outcome"):
        if step_status == status.ACTIVE:
            open_proposals.add(proposal_id)
        if instance_id in masked:
            outcome = ""
        by_proposal[proposal_id].append(
            (
                key,
                outcome if step_status == status.COMPLETED and outcome else step_status,
            )
        )
    counts = Counter()
    finished = {}
    for proposal_id, path in by_proposal.items():
        path = tuple(path)
        counts[path] += 1
        finished[path] = finished.get(path, True) and proposal_id not in open_proposals
    scenarios = scenario_paths(call)
    return {
        "round": call_round.uuid.hex if call_round is not None else None,
        "scenarios_known": scenarios is not None,
        "paths": [
            {
                "steps": [{"key": key, "result": result} for key, result in path],
                "proposals": count,
                "finished": finished[path],
                "covered_by_scenario": _covered(path, scenarios)
                if scenarios is not None
                else None,
            }
            for path, count in counts.most_common()
        ],
    }


def proposal_email_logs(proposal, uuids):
    """The email log rows among ``uuids`` that ``proposal``'s workflow mail
    wrote: never another proposal's, whatever is asked for."""
    logged = {
        uuid
        for event in notification_events(proposal)
        for uuid in event.context.get("email_log_uuids") or []
    }
    wanted = [uuid for uuid in uuids if uuid in logged]
    return logging_models.EmailLog.objects.filter(uuid__in=wanted).order_by(
        "sent_at", "id"
    )
