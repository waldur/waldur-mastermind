"""A round's steps: due dates per round, steps run once per round, and
moving a round's proposals on together.

- ``set_due_date``: the date a step is due in a round, for every proposal of
  it (``RoundStep.due_at``); the deadlines of the proposals at the step move
  with it.
- ``open_step``: the call's managers open a round step for the round. Refused
  while proposals of the round are still before it, unless forced with a
  reason; the proposals parked just before it are moved into it, and its
  contributors' round reports are created (``round_reports``).
- ``submit_report`` / ``reopen_report``: a report is submitted by its author;
  a proposal whose reports are all in has its assessment step completed.
- ``advance_for_round``: move on every proposal of the round parked on a
  completed step of manual transition.

Each proposal is moved in its own transaction, under its own row lock, as
``round_lifecycle.complete_step_for_round`` does: one that fails is listed
and the rest of the round goes on. The callers hold no transaction.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.checklist import models as checklist_models
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.proposal import (
    models,
    rankings,
    round_lifecycle,
    round_reports,
    tasks,
    utils,
    workflow_service,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    CallStates,
    NotificationRuleTriggers,
    ProposalStates,
    RoundLifecycleStates,
    RoundStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

logger = logging.getLogger(__name__)

UNDER_EVALUATION = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)


class LaggingProposalsError(exceptions.ValidationError):
    """Opening a round step is refused: proposals have not reached it."""

    default_code = "lagging_proposals"

    def __init__(self, lagging_count):
        self.lagging_count = lagging_count
        super().__init__(
            _(
                "%(count)s proposal(s) of this round have not reached the step "
                "yet. Open it once they have, or open it now with force and a "
                "reason; they join it as they arrive."
            )
            % {"count": lagging_count}
        )


# =============================================================================
# Where the round's proposals stand
# =============================================================================


def _would_enter(proposal, target):
    """Whether moving ``proposal`` on from the step it is parked on starts
    ``target``, its instance of a step: no step still to run lies between."""
    current = proposal.current_step
    for member in workflow_service.next_stage_preview(proposal, current):
        if member.pk == target.pk:
            return True
    return False


def standing(call_round, call_step):
    """Where each proposal of the round under evaluation stands against
    ``call_step``: a dict of lists of proposal ids -- ``at`` the step,
    ``parked`` just before it waiting to be moved into it, ``before`` it
    otherwise, and ``awaiting_advance`` parked on it once completed."""
    result = {"at": [], "parked": [], "before": [], "awaiting_advance": []}
    proposals = (
        models.Proposal.objects.filter(round=call_round, state__in=UNDER_EVALUATION)
        .select_related("current_step__call_step")
        .order_by("pk")
    )
    instances = {
        instance.proposal_id: instance
        for instance in models.ProposalWorkflowStepInstance.objects.filter(
            proposal__round=call_round, call_step=call_step
        )
    }
    for proposal in proposals:
        instance = instances.get(proposal.pk)
        if instance is None or proposal.decision_held:
            continue
        if instance.status == WorkflowStepInstanceStatuses.ACTIVE:
            if proposal.state == ProposalStates.IN_REVIEW:
                result["at"].append(proposal.pk)
            continue
        if instance.status == WorkflowStepInstanceStatuses.COMPLETED:
            current = proposal.current_step
            if (
                current is not None
                and current.stage_key == instance.stage_key
                and workflow_service.is_awaiting_manual_advance(proposal)
            ):
                result["awaiting_advance"].append(proposal.pk)
            continue
        if instance.status != WorkflowStepInstanceStatuses.PENDING:
            continue
        if (
            proposal.state == ProposalStates.IN_REVIEW
            and workflow_service.is_awaiting_manual_advance(proposal)
            and _would_enter(proposal, instance)
        ):
            result["parked"].append(proposal.pk)
        else:
            result["before"].append(proposal.pk)
    return result


def _ensure_round_running(call_round):
    if call_round.call.state != CallStates.ACTIVE:
        raise exceptions.ValidationError(_("Call is not active."))
    if call_round.status != RoundStatuses.ENDED:
        raise exceptions.ValidationError(
            _("The round has not reached its cut-off yet.")
        )
    if call_round.lifecycle_state in RoundLifecycleStates.PUBLISHED:
        raise exceptions.ValidationError(
            _("The round's results are published; its steps are over.")
        )


# =============================================================================
# Moving proposals on
# =============================================================================


def _entry(proposal, detail):
    return {
        "proposal_uuid": proposal.uuid,
        "proposal_name": proposal.name,
        "detail": detail,
        "proposal_state": proposal.state,
        "current_step_name": workflow_service.standing_label(proposal),
    }


def _advance_one(proposal_id, user, target=None):
    """Move one parked proposal on, as the advance action on the proposal
    does; with ``target``, only into that call step. The caller holds a
    transaction. Returns ``(advanced, entry)``."""
    proposal = (
        models.Proposal.objects.select_for_update(of=("self",))
        .select_related("round__call__manager__customer")
        .get(pk=proposal_id)
    )
    if proposal.state != ProposalStates.IN_REVIEW or not (
        workflow_service.is_awaiting_manual_advance(proposal)
    ):
        return False, _entry(proposal, _("Not waiting to be moved on."))
    if not (
        user.is_staff or proposal_permissions.manages_proposal_workflow(user, proposal)
    ):
        return False, _entry(proposal, _("You cannot move this proposal on."))
    if target is not None:
        instance = proposal.workflow_step_instances.filter(call_step=target).first()
        if instance is None or not _would_enter(proposal, instance):
            return False, _entry(proposal, _("Not waiting to enter this step."))
    from_step = proposal.workflow_step
    next_instance = workflow_service.advance_step(proposal, acting_user=user)
    event_logger.emit(
        "Proposal {proposal_name} workflow manually advanced "
        "from {from_step} to {target} by {actor}.",
        event_type=EventType.PROPOSAL_WORKFLOW_ADVANCED,
        event_context={
            "proposal": proposal,
            "from_step": from_step,
            "target": (
                "workflow completion" if next_instance is None else next_instance.step
            ),
            "actor": user.full_name or user.username,
        },
        scopes=[proposal],
    )
    proposal.refresh_from_db()
    if next_instance is None and not proposal.decision_held:
        transaction.on_commit(
            lambda proposal_uuid=proposal.uuid, state=proposal.state: (
                tasks.notify_proposal_decision(
                    proposal_uuid, ProposalStates.IN_REVIEW, state
                )
            )
        )
    return True, _entry(proposal, _("Moved on."))


def _for_each(proposal_ids, action, label):
    """Run ``action(proposal_id)`` per proposal in its own transaction."""
    done, skipped, failed = [], [], []
    for proposal_id in proposal_ids:
        try:
            with transaction.atomic():
                moved, entry = action(proposal_id)
        except Exception as error:
            if not isinstance(
                error, (exceptions.ValidationError, DjangoValidationError, ValueError)
            ):
                logger.exception("%s failed for proposal id=%s", label, proposal_id)
            proposal = models.Proposal.objects.get(pk=proposal_id)
            failed.append(_entry(proposal, round_lifecycle._failure_detail(error)))
            continue
        (done if moved else skipped).append(entry)
    return done, skipped, failed


def advance_for_round(call_round, call_step, user):
    """Move on every proposal of the round parked on ``call_step`` once
    completed, as a manual advance on each would. Running it again changes
    nothing. Returns ``(advanced, skipped, failed)``."""
    _ensure_round_running(call_round)
    proposal_ids = standing(call_round, call_step)["awaiting_advance"]
    return _for_each(
        proposal_ids,
        lambda proposal_id: _advance_one(proposal_id, user),
        f"Advancing past step {call_step.uuid.hex}",
    )


# =============================================================================
# Due dates per round
# =============================================================================


def _lock_round_step(call_round, call_step):
    round_step, _created = models.RoundStep.objects.get_or_create(
        round=call_round, call_step=call_step
    )
    return (
        models.RoundStep.objects.select_for_update()
        .select_related("round__call", "call_step")
        .get(pk=round_step.pk)
    )


def _rearm(instance, deadline):
    """Give the active ``instance`` its new deadline, its open slots with
    it, and send its deadline reminders again against it."""
    previous = instance.deadline
    if previous == deadline:
        return
    instance.deadline = deadline
    instance.sent_notifications = [
        key
        for key in instance.sent_notifications
        if not key.startswith(NotificationRuleTriggers.DEADLINE_APPROACHING)
    ]
    instance.save(update_fields=["deadline", "sent_notifications"])
    instance.contributions.filter(due_at=previous).update(due_at=deadline)


def refresh_deadlines(round_step):
    """Bring the deadline of every proposal at the step in the round in line
    with the round's record of it. The caller holds the round step's lock."""
    instances = models.ProposalWorkflowStepInstance.objects.select_for_update(
        of=("self",)
    ).filter(
        proposal__round=round_step.round,
        call_step=round_step.call_step,
        status=WorkflowStepInstanceStatuses.ACTIVE,
    )
    for instance in instances.select_related("call_step"):
        _rearm(instance, workflow_service.step_deadline(instance, round_step))


@transaction.atomic
def set_due_date(call_round, call_step, due_at):
    """Set when ``call_step`` is due in the round, for every proposal; None
    counts the step's duration again. The proposals at the step get the new
    deadline at once, and their deadline reminders are sent again for it."""
    call_round = models.Round.objects.select_related("call").get(pk=call_round.pk)
    if call_round.lifecycle_state == RoundLifecycleStates.CLOSED:
        raise exceptions.ValidationError(_("The round is closed."))
    round_step = _lock_round_step(call_round, call_step)
    round_step.due_at = due_at
    round_step.save(update_fields=["due_at", "modified"])
    refresh_deadlines(round_step)
    # The reports' reminders are sent again against the new date.
    round_step.sent_notifications = [
        key
        for key in round_step.sent_notifications
        if not key.startswith(tasks.ROUND_REPORT_REMINDER_PREFIX)
    ]
    round_step.save(update_fields=["sent_notifications"])
    return round_step


# =============================================================================
# Opening a round step
# =============================================================================


def open_step(call_round, call_step, user, due_at=None, force=False, reason=""):
    """Open the round step ``call_step`` for the round.

    Refused while a proposal of the round under evaluation is still before
    the step, unless ``force``: the batch is the round's. The proposals
    parked on the step before it are moved into it, each in its own
    transaction; then the step is opened, the proposals at it get its
    deadline, and its contributors' round reports are created, each author
    and the call's managers told once. Running it again opens nothing anew:
    a proposal that arrived meanwhile is moved in and joins its reports.
    Returns ``(round_step, created_reports, advanced, skipped, failed)``.
    """
    _ensure_round_running(call_round)
    where = standing(call_round, call_step)
    if where["before"] and not force:
        raise LaggingProposalsError(len(where["before"]))
    with transaction.atomic():
        round_step = _lock_round_step(call_round, call_step)
        if due_at is not None:
            round_step.due_at = due_at
            round_step.save(update_fields=["due_at", "modified"])
    advanced, skipped, failed = _for_each(
        where["parked"],
        lambda proposal_id: _advance_one(proposal_id, user, target=call_step),
        f"Opening round step {call_step.uuid.hex}",
    )
    with transaction.atomic():
        round_step = _lock_round_step(call_round, call_step)
        first = not round_step.is_open
        if first:
            round_step.opened_at = timezone.now()
            round_step.opened_by = user
            round_step.opened_reason = reason if where["before"] else ""
            round_step.save(
                update_fields=["opened_at", "opened_by", "opened_reason", "modified"]
            )
        refresh_deadlines(round_step)
        created = round_reports.sync_reports(round_step)
        if created:
            round_reports.announce_reports(round_step, created)
        if first:
            utils.emit_round_event(
                call_round,
                "Step {step_name} of {round_name} of call {call_name} was opened "
                "for the round by {actor}.",
                EventType.ROUND_STEP_OPENED,
                step_name=call_step.name or call_step.key,
                actor=user.full_name or user.username,
                forced_reason=round_step.opened_reason,
            )
            round_step_uuid = round_step.uuid.hex
            transaction.on_commit(
                lambda: tasks.notify_call_managers_about_round_step.delay(
                    round_step_uuid
                )
            )
    logger.info(
        "Round step %s opened for round %s by user %s: %s report(s) created, "
        "%s advanced, %s skipped, %s failed",
        call_step.uuid.hex,
        call_round.uuid.hex,
        user.uuid.hex,
        len(created),
        len(advanced),
        len(skipped),
        len(failed),
    )
    return round_step, created, advanced, skipped, failed


# =============================================================================
# Round reports
# =============================================================================


def report_completion(note):
    """The completion of the report's step form, made on first use; None for
    a report whose step has no form."""
    checklist = note.call_step.checklist if note.call_step_id else None
    if checklist is None:
        return None
    completion, _created = checklist_models.ChecklistCompletion.objects.get_or_create(
        scope_content_type=ContentType.objects.get_for_model(models.RoundMeetingNote),
        scope_object_id=note.pk,
        checklist=checklist,
    )
    return completion


def unanswered_required_questions(note):
    completion = report_completion(note)
    if completion is None or not note.call_step.checklist_required:
        return checklist_models.Question.objects.none()
    answered = completion.answers.values_list("question_id", flat=True)
    return note.call_step.checklist.questions.filter(required=True).exclude(
        id__in=answered
    )


def _lock_report(note):
    return (
        models.RoundMeetingNote.objects.select_for_update(of=("self",))
        .select_related("round__call__manager__customer", "call_step", "author")
        .get(pk=note.pk)
    )


def _open_round_step(note):
    round_step = round_reports.round_step_of(note.round, note.call_step)
    if round_step is None or not round_step.is_open:
        raise exceptions.ValidationError(_("The round step is not open."))
    return round_step


def submit_report(note, user):
    """Submit the round report ``note`` as its author.

    Its form's required questions must be answered and it must cover a
    proposal. What it covers is then fixed, and its ranking with it. Every
    covered proposal whose reports are all in has its assessment step
    completed (``complete_covered``). Returns ``(note, completed, failed)``.
    """
    with transaction.atomic():
        note = _lock_report(note)
        if not note.is_report:
            raise exceptions.ValidationError(_("This is not a round report."))
        if note.author_id != user.pk:
            raise exceptions.PermissionDenied(
                _("Only the author of a round report submits it.")
            )
        if note.submitted_at is not None:
            raise exceptions.ValidationError(_("The report is already submitted."))
        _open_round_step(note)
        if not note.proposals.exists():
            raise exceptions.ValidationError(
                _("The report covers no proposal, so there is nothing to submit.")
            )
        # The author no longer holding what they wrote it through does not
        # submit it.
        if not round_reports.writes_for(user, note):
            raise exceptions.PermissionDenied(
                _("You no longer write this round report.")
            )
        unanswered = unanswered_required_questions(note).count()
        if unanswered:
            raise exceptions.ValidationError(
                _("%(count)s required question(s) of the report's form are unanswered.")
                % {"count": unanswered}
            )
        note.submitted_at = timezone.now()
        note.save(update_fields=["submitted_at", "modified"])
        utils.emit_round_event(
            note.round,
            "{author} submitted the round report of step {step_name} of "
            "{round_name} of call {call_name}.",
            EventType.ROUND_REPORT_SUBMITTED,
            step_name=note.call_step.name or note.call_step.key,
            author=user.full_name or user.username,
            report_uuid=note.uuid.hex,
        )
    completed, failed = complete_covered(note, user)
    return note, completed, failed


def reopen_report(note, user):
    """Hand a submitted round report back to its author, for the call's
    managers: while the round step is open and proposals stand at it, so
    that a proposal arriving after the report was submitted joins it."""
    with transaction.atomic():
        note = _lock_report(note)
        if not note.is_report:
            raise exceptions.ValidationError(_("This is not a round report."))
        if not rankings.can_write(user, note.round):
            raise exceptions.PermissionDenied(
                _("Only the call's managers reopen a round report.")
            )
        if note.submitted_at is None:
            raise exceptions.ValidationError(_("The report is not submitted."))
        round_step = _open_round_step(note)
        rankings.ensure_ranking_open(note.round)
        if not round_reports.instances_at(note.round, note.call_step).exists():
            raise exceptions.ValidationError(
                _("No proposal of the round stands at the step any more.")
            )
        note.submitted_at = None
        note.save(update_fields=["submitted_at", "modified"])
        round_step = models.RoundStep.objects.select_for_update().get(pk=round_step.pk)
        round_reports.sync_reports(round_step)
        utils.emit_round_event(
            note.round,
            "{actor} reopened a round report of step {step_name} of "
            "{round_name} of call {call_name}.",
            EventType.ROUND_REPORT_REOPENED,
            step_name=note.call_step.name or note.call_step.key,
            actor=user.full_name or user.username,
            report_uuid=note.uuid.hex,
        )
    return note


def completes_by_reports(call_step) -> bool:
    """Whether submitting the round step's reports completes it: an
    assessment that ends ``done``. A decision is the call's managers' to
    complete for the round."""
    return (
        call_step.kind == StepKinds.ASSESSMENT
        and WorkflowStepOutcomes.DONE in call_step.allowed_outcomes
    )


def _complete_if_reported(proposal_id, call_step, user):
    proposal = (
        models.Proposal.objects.select_for_update(of=("self",))
        .select_related("round__call__manager__customer")
        .get(pk=proposal_id)
    )
    instance = (
        proposal.workflow_step_instances.select_for_update(of=("self",))
        .select_related("call_step", "proposal__round__call")
        .filter(call_step=call_step, status=WorkflowStepInstanceStatuses.ACTIVE)
        .first()
        if proposal.state == ProposalStates.IN_REVIEW
        else None
    )
    if instance is None:
        return False, _entry(proposal, _("Not at this step."))
    if not round_reports.reports_done(instance):
        return False, _entry(proposal, _("Waiting for other round reports."))
    next_instance = workflow_service.complete_step(
        proposal=proposal,
        current_instance=instance,
        outcome=WorkflowStepOutcomes.DONE,
        outcome_reason="",
        completed_by=user,
    )
    proposal.refresh_from_db()
    if next_instance is None and proposal.state in (
        ProposalStates.ACCEPTED,
        ProposalStates.REJECTED,
        ProposalStates.CANCELED,
    ):
        transaction.on_commit(
            lambda proposal_uuid=proposal.uuid, state=proposal.state: (
                tasks.notify_proposal_decision(
                    proposal_uuid, ProposalStates.IN_REVIEW, state
                )
            )
        )
    return True, _entry(proposal, _("Completed."))


def complete_covered(note, user):
    """Complete the round step for the proposals ``note`` covers whose
    reports are all submitted, with ``done``, by the report's author.

    Only for an assessment step, only the proposals the submitted report
    covers, and only once every author writing for a proposal has submitted
    a report covering it. Returns ``(completed, failed)``.
    """
    call_step = note.call_step
    if not completes_by_reports(call_step) or note.submitted_at is None:
        return [], []
    proposal_ids = list(note.proposals.order_by("pk").values_list("pk", flat=True))
    completed, _skipped, failed = _for_each(
        proposal_ids,
        lambda proposal_id: _complete_if_reported(proposal_id, call_step, user),
        f"Completing round step {call_step.uuid.hex}",
    )
    return completed, failed
