"""Round reports: the work of a step run once per round.

A round step (``CallWorkflowStep.round_scoped``) has no contributions on each
proposal. Each of its contributors writes one round report instead -- a
``RoundMeetingNote`` with the step -- covering the proposals of the round
they hold the step for: a domain panel chair the proposals of their domain,
the call's panel chair and vice-chair every proposal at the step. A step
without contributors has no reports; the call's managers complete it for the
round.

Who writes a report is resolved per proposal as for the step's own
contributors, so nobody covers a proposal they are on the team of or have a
conflict of interest with. Kept apart from the workflow engine, which calls
in here when a proposal reaches a round step already opened.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, F, Q

from waldur_core.permissions.utils import get_users
from waldur_mastermind.proposal import models, proposal_roles
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    ProposalStates,
    ResponsibleRoles,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    parse_contributor_source,
)

User = get_user_model()


def round_step_of(call_round, call_step):
    """The round's record of ``call_step``, or None."""
    return models.RoundStep.objects.filter(
        round=call_round, call_step=call_step
    ).first()


def report_authors(instance):
    """Who writes a round report covering the proposal of ``instance``, the
    round step standing on it.

    The holders of the evaluator role, or of the call role, the step names
    as its contributors, resolved for this proposal: never someone on its
    applicant team or with a conflict of interest recorded with it.
    """
    call_step = instance.call_step
    proposal = instance.proposal
    try:
        source, parameter = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        return []
    team = proposal_roles.applicant_team_user_ids(proposal)
    if source == ContributorSources.PROPOSAL_ROLE:
        users = proposal_roles.proposal_role_holders(
            proposal,
            call_step.contributor_source,
            lead_only=call_step.contributors_lead_only,
        )
        return [user for user in users if user.pk not in team]
    if source == ContributorSources.CALL_ROLE:
        call = proposal.round.call
        if parameter == ResponsibleRoles.PANEL_CHAIR:
            return list(
                User.objects.filter(
                    pk__in=proposal_permissions.panel_chair_ids(call, proposal)
                )
            )
        role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE.get(parameter)
        if not role_name:
            return []
        return [
            user
            for user in get_users(call, role_name=role_name)
            if user.pk not in team
            and proposal_permissions.holds_call_role_on_proposal(
                user, proposal, role_name
            )
        ]
    return []


def writes_for(user, note) -> bool:
    """Whether ``user`` still writes the round report ``note``: its author,
    and still one of the authors of a proposal it covers. Whoever lost the
    role they wrote it through -- taken off the call or the panel, their
    evaluator role revoked, a conflict recorded -- no longer does."""
    if note.author_id != user.pk:
        return False
    instances = instances_at(note.round, note.call_step).filter(
        proposal__in=note.proposals.all()
    )
    return any(
        author.pk == user.pk
        for instance in instances
        for author in report_authors(instance)
    )


def instances_at(call_round, call_step):
    """The round's proposals standing at the round step ``call_step``."""
    return models.ProposalWorkflowStepInstance.objects.filter(
        proposal__round=call_round,
        proposal__state=ProposalStates.IN_REVIEW,
        call_step=call_step,
        status=WorkflowStepInstanceStatuses.ACTIVE,
    ).select_related("call_step", "proposal__round__call")


def reports_of(call_round, call_step):
    return models.RoundMeetingNote.objects.filter(round=call_round, call_step=call_step)


def report_title(call_step, user):
    return f"{call_step.name or call_step.key} — {user.full_name or user.username}"


def sync_reports(round_step):
    """Bring the opened round step's draft reports in line with who holds
    which of its proposals now.

    Each author's draft covers exactly the proposals at the step they write
    for; their ranking of a proposal no longer covered goes with it. A
    report already submitted is left as it is: a proposal reaching the step
    after it waits until the call's managers reopen it. Returns the reports
    created. The caller holds the round step's row lock.
    """
    if not round_step.is_open:
        return []
    call_round, call_step = round_step.round, round_step.call_step
    coverage, authors = {}, {}
    for instance in instances_at(call_round, call_step):
        for user in report_authors(instance):
            coverage.setdefault(user.pk, set()).add(instance.proposal_id)
            authors[user.pk] = user
    created = []
    reports = {
        report.author_id: report
        for report in reports_of(call_round, call_step).select_for_update()
    }
    for author_id, proposal_ids in coverage.items():
        report = reports.get(author_id)
        if report is None:
            report = models.RoundMeetingNote.objects.create(
                round=call_round,
                call_step=call_step,
                author=authors[author_id],
                title=report_title(call_step, authors[author_id]),
            )
            report.proposals.set(proposal_ids)
            created.append(report)
        elif report.submitted_at is None:
            report.proposals.set(proposal_ids)
            report.rankings.exclude(proposal_id__in=proposal_ids).delete()
    for author_id, report in reports.items():
        if author_id not in coverage and report.submitted_at is None:
            report.proposals.clear()
            report.rankings.all().delete()
    return created


def missing_reports(instance):
    """The authors who have yet to submit a report covering the proposal of
    ``instance``, and those whose report was submitted without it, as two
    lists of users."""
    authors = report_authors(instance)
    if not authors:
        return [], []
    reports = {
        report.author_id: report
        for report in reports_of(
            instance.proposal.round, instance.call_step
        ).prefetch_related("proposals")
    }
    pending, closed = [], []
    for user in authors:
        report = reports.get(user.pk)
        if report is None or report.submitted_at is None:
            pending.append(user)
        elif not any(p.pk == instance.proposal_id for p in report.proposals.all()):
            closed.append(user)
    return pending, closed


def reports_done(instance) -> bool:
    """Whether every author of the proposal at the round step has submitted
    a report covering it, and there is at least one."""
    if not report_authors(instance):
        return False
    pending, closed = missing_reports(instance)
    return not pending and not closed


def why_round_step_waits(instance) -> str | None:
    """Why a proposal stands at a round step, for the call managers."""
    call_step = instance.call_step
    round_step = round_step_of(instance.proposal.round, call_step)
    if round_step is None or not round_step.is_open:
        return (
            "Waiting for the call managers to open this step for the whole "
            "round, once the round's proposals have reached it."
        )
    if call_step.contributor_source_kind == ContributorSources.NONE:
        return None
    if not report_authors(instance):
        return (
            "Nobody can write a round report covering this proposal: assign "
            "a holder of the step's role, or complete the step for the round "
            "as a call manager."
        )
    pending, closed = missing_reports(instance)
    if closed:
        names = ", ".join(user.full_name or user.username for user in closed)
        return (
            f"The round report of {names} was submitted before this proposal "
            "reached the step: reopen it to add the proposal."
        )
    if pending:
        count = len(pending)
        return f"Waiting for {count} round {'report' if count == 1 else 'reports'}."
    return None


def announce_reports(round_step, reports):
    """Ask each author of ``reports`` for their round report, once the
    transaction commits: one mail each, however many proposals it covers."""
    from waldur_mastermind.proposal import tasks

    uuids = [report.uuid.hex for report in reports]
    transaction.on_commit(lambda: tasks.notify_round_report_authors.delay(uuids))


UNDER_EVALUATION = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)


def round_steps_by_round(call, round_ids):
    """Per round of ``call`` in ``round_ids``, per enabled step of the call:
    its due date in the round, how many of the round's proposals stand
    before it, at it, and parked on it once completed; and for a round step
    its opening and its reports. A fixed number of queries per call, so a
    list of rounds does not run them per round."""
    steps = list(
        models.CallWorkflowStep.objects.filter(call=call, is_enabled=True).order_by(
            "order", "id"
        )
    )
    records = {
        (record.round_id, record.call_step_id): record
        for record in models.RoundStep.objects.filter(round_id__in=round_ids)
    }
    instances = models.ProposalWorkflowStepInstance.objects.filter(
        proposal__round_id__in=round_ids,
        proposal__state__in=UNDER_EVALUATION,
        proposal__decision_held=False,
    )
    counts = {
        (row["proposal__round_id"], row["call_step_id"], row["status"]): row["n"]
        for row in instances.values("proposal__round_id", "call_step_id", "status")
        .annotate(n=Count("id"))
        .order_by()
    }
    awaiting = {
        (row["proposal__round_id"], row["call_step_id"]): row["n"]
        for row in instances.filter(
            status=WorkflowStepInstanceStatuses.COMPLETED,
            call_step__transition_mode=TransitionModes.MANUAL,
            proposal__state=ProposalStates.IN_REVIEW,
            proposal__current_step__status=WorkflowStepInstanceStatuses.COMPLETED,
            proposal__current_step__stage=F("stage"),
            proposal__current_step__call_step__transition_mode=TransitionModes.MANUAL,
        )
        .values("proposal__round_id", "call_step_id")
        .annotate(n=Count("id"))
        .order_by()
    }
    reports = {}
    for row in (
        models.RoundMeetingNote.objects.filter(
            round_id__in=round_ids, call_step__isnull=False
        )
        .exclude(proposals=None)
        .values("round_id", "call_step_id")
        .annotate(
            total=Count("id", distinct=True),
            submitted=Count("id", distinct=True, filter=Q(submitted_at__isnull=False)),
        )
        .order_by()
    ):
        reports[(row["round_id"], row["call_step_id"])] = row
    result = {}
    for round_id in round_ids:
        rows = []
        for call_step in steps:
            key = (round_id, call_step.pk)
            record = records.get(key)
            report = reports.get(key, {})
            rows.append(
                {
                    "call_step": call_step,
                    "has_reports": call_step.round_scoped
                    and call_step.collects_contributions,
                    "due_at": record.due_at if record else None,
                    "opened_at": record.opened_at if record else None,
                    "proposals_at_step": counts.get(
                        (*key, WorkflowStepInstanceStatuses.ACTIVE), 0
                    ),
                    "proposals_before_step": counts.get(
                        (*key, WorkflowStepInstanceStatuses.PENDING), 0
                    ),
                    "proposals_awaiting_advance": awaiting.get(key, 0),
                    "reports_total": report.get("total", 0),
                    "reports_submitted": report.get("submitted", 0),
                }
            )
        result[round_id] = rows
    return result


def round_steps_of(call_round):
    return round_steps_by_round(call_round.call, [call_round.pk])[call_round.pk]
