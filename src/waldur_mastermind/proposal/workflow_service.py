"""Workflow step transition helpers.

Centralises the logic that advances a proposal's workflow when a step is
completed, rejected, or expires. Callers must hold a row-level lock on the
active step instance (via select_for_update) before invoking these helpers.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Avg
from django.utils import timezone

from waldur_core.core.utils import get_system_robot
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.proposal import models, notification_rules, utils
from waldur_mastermind.proposal.enums import (
    WORKFLOW_STEPS,
    WORKFLOW_STEPS_MAP,
    NotificationRuleTriggers,
    ProposalStates,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

logger = logging.getLogger(__name__)

DECISION_STEP = "allocation_decision"


def _step_label(step_key):
    """Human-readable step name for error messages (falls back to the raw key)."""
    step_def = WORKFLOW_STEPS_MAP.get(step_key)
    return step_def.name if step_def else step_key


def _merge_notes(existing, addition):
    """Append a new note to existing internal notes with a blank-line break.

    Notes accumulate across the step's lifecycle (e.g. a manager records a
    holding note before deciding on the outcome). Overwriting on every save
    would silently discard earlier context.
    """
    if not existing:
        return addition
    return f"{existing}\n\n{addition}"


def _next_enabled_step(proposal, current_step_id):
    """Return the next step the proposal should advance to.

    Source of truth is the ``ProposalWorkflowStepInstance`` set fixed at
    submit. Live edits to the call's ``CallWorkflowStep`` config (enabling
    a new step, disabling a downstream step) do not retroactively alter a
    proposal's path. Walks the catalog in order and returns the first step
    whose instance is PENDING.
    """
    pending_step_ids = set(
        proposal.workflow_step_instances.filter(
            status=WorkflowStepInstanceStatuses.PENDING
        ).values_list("step", flat=True)
    )
    step_indexes = {step.id: i for i, step in enumerate(WORKFLOW_STEPS)}
    index = step_indexes.get(current_step_id)
    if index is None:
        return None
    for step_def in WORKFLOW_STEPS[index + 1 :]:
        if step_def.id in pending_step_ids:
            return step_def
    return None


def _activate_next_step(proposal, next_step_def):
    """Activate the next pending step instance, set its deadline, return it.

    The instance is guaranteed to exist because ``_next_enabled_step`` selects
    only steps backed by an instance. Duration is read live from
    ``CallWorkflowStep`` so admins can adjust deadlines before activation.
    """
    instance = proposal.workflow_step_instances.get(step=next_step_def.id)
    call_step = models.CallWorkflowStep.objects.filter(
        call=proposal.round.call, step=next_step_def.id
    ).first()
    instance.status = WorkflowStepInstanceStatuses.ACTIVE
    instance.started_at = timezone.now()
    if call_step and call_step.duration_in_days:
        instance.deadline = instance.started_at + timedelta(
            days=call_step.duration_in_days
        )
    # Provision a checklist completion for the step so its responsible role has
    # something to answer against once the step is active.
    if call_step and call_step.checklist_id:
        proposal.ensure_checklist_completion_for(call_step.checklist)
    # The allocation decision edits an award, which starts as the request.
    if next_step_def.id == DECISION_STEP:
        utils.prefill_awarded_resources(proposal)
    instance.save(update_fields=["status", "started_at", "deadline"])
    notification_rules.dispatch_step_event(
        instance, NotificationRuleTriggers.STEP_STARTED
    )
    return instance


def _decision_is_withheld(proposal, step):
    """Whether the outcome of ``step`` must be held until the round publishes.

    Locks the round row, so a decision and the round's publication serialise:
    a decision either lands before publication and is released by it, or
    after and is announced at once. Publication locks the round's proposals
    before the round, and every caller here holds the proposal's row lock
    before it gets here (the step actions in views, the expiry sweep in
    tasks), so the order is proposal, then round everywhere and the two
    cannot deadlock.
    """
    if step != DECISION_STEP:
        return False
    call_round = (
        models.Round.objects.select_for_update()
        .select_related("call")
        .get(pk=proposal.round.pk)
    )
    return call_round.withholds_results


def _hold_decision(proposal):
    """Keep a decided proposal ``in_review`` until its round publishes."""
    proposal.decision_held = True
    proposal.save(update_fields=["decision_held"])


def create_step_instances(proposal):
    """Fix the proposal's workflow path at submission.

    One instance per catalog step: ``pending`` where the call has the step
    enabled, ``skipped`` otherwise. Later edits to the call's step
    configuration do not alter this path (see ``_next_enabled_step``).
    """
    enabled_step_ids = set(
        models.CallWorkflowStep.objects.filter(
            call=proposal.round.call, is_enabled=True
        ).values_list("step", flat=True)
    )
    models.ProposalWorkflowStepInstance.objects.bulk_create(
        [
            models.ProposalWorkflowStepInstance(
                proposal=proposal,
                step=step_def.id,
                status=(
                    WorkflowStepInstanceStatuses.PENDING
                    if step_def.id in enabled_step_ids
                    else WorkflowStepInstanceStatuses.SKIPPED
                ),
            )
            for step_def in WORKFLOW_STEPS
        ]
    )


def _first_pending_step(proposal):
    pending_step_ids = set(
        proposal.workflow_step_instances.filter(
            status=WorkflowStepInstanceStatuses.PENDING
        ).values_list("step", flat=True)
    )
    return next((s for s in WORKFLOW_STEPS if s.id in pending_step_ids), None)


def activate_first_step(proposal):
    """Start a submitted proposal's evaluation: activate its first pending step.

    Shared by submission (when the call evaluates on submission) and by the
    cut-off task (when it evaluates at the round's cut-off). The deadline runs
    from now, and the step-started notification rules fire through their
    ledger. Moves the proposal to ``in_review``. Returns the active instance,
    or None when the proposal has no pending step, in which case it is left
    as it is. The caller holds the proposal's row lock inside a transaction.
    """
    step_def = _first_pending_step(proposal)
    if step_def is None:
        return None
    instance = _activate_next_step(proposal, step_def)
    proposal.state = ProposalStates.IN_REVIEW
    proposal.workflow_step = step_def.id
    proposal.save(update_fields=["state", "workflow_step"])
    return instance


@transaction.atomic
def complete_step(
    proposal,
    current_instance,
    outcome,
    outcome_reason,
    completed_by,
    internal_notes="",
):
    """Complete the active step. Advance only if transition_mode is automatic.

    Returns the newly active next step instance, or None if the workflow
    terminated (proposal accepted) OR if the step is awaiting manual advance.
    Callers should inspect ``is_awaiting_manual_advance(proposal)`` to
    distinguish those two cases.

    ``internal_notes`` is a call-management-team-only free-text field; it is
    appended (not overwritten) so completing a step never erases notes
    captured earlier in the same step's lifecycle.

    Raises ``ValueError`` if the step's review gate (``min_reviewers`` /
    ``min_score_threshold``) is configured and not yet satisfied.
    """
    call_step = models.CallWorkflowStep.objects.filter(
        call=proposal.round.call, step=current_instance.step
    ).first()
    _enforce_step_gates(proposal, current_instance, outcome, call_step)
    _enforce_award_amounts(proposal, current_instance, outcome)

    current_instance.status = WorkflowStepInstanceStatuses.COMPLETED
    current_instance.outcome = outcome
    current_instance.outcome_reason = outcome_reason or ""
    current_instance.completed_at = timezone.now()
    current_instance.completed_by = completed_by
    if internal_notes:
        current_instance.internal_notes = _merge_notes(
            current_instance.internal_notes, internal_notes
        )
    current_instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "internal_notes",
            "completed_at",
            "completed_by",
        ]
    )
    if _decision_is_withheld(proposal, current_instance.step):
        # Recorded on the instance; the step's notifications, the proposal's
        # final state and anything that follows wait for publication.
        _hold_decision(proposal)
        return None

    # A negative outcome ends the workflow, so it is a rejection for
    # notification purposes even though it arrives through complete_step.
    notification_rules.dispatch_step_event(
        current_instance,
        NotificationRuleTriggers.STEP_REJECTED
        if outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES
        else NotificationRuleTriggers.STEP_COMPLETED,
    )

    # A negative outcome (declined / ineligible / infeasible) terminates the
    # workflow instead of advancing or allocating — regardless of transition
    # mode or whether downstream steps remain. The applicant declining the
    # award cancels the proposal; any other negative decision rejects it. The
    # instance keeps its true outcome (unlike reject_at_step, which overwrites
    # it with REJECTED).
    if outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES:
        if (
            outcome == WorkflowStepOutcomes.DECLINED
            and current_instance.step == "award_response"
        ):
            proposal.state = ProposalStates.CANCELED
        else:
            proposal.state = ProposalStates.REJECTED
        proposal.workflow_step = None
        proposal.save(update_fields=["state", "workflow_step"])
        return None

    if call_step and call_step.transition_mode == TransitionModes.MANUAL:
        # Keep proposal.workflow_step pointing at this step so the manual
        # advance endpoint and UI can locate it.
        return None

    return _advance_to_next(proposal, current_instance.step, acting_user=completed_by)


def _enforce_step_gates(proposal, current_instance, outcome, call_step):
    """Block completion of a gated step until its review thresholds are met.

    ``CallWorkflowStep.min_reviewers`` / ``min_score_threshold`` are the single
    enforced source of truth for "enough reviewers" and "high enough average
    score" (replacing the removed Round-level fields). They are evaluated
    against the proposal's *submitted* reviews. A ``DECLINED`` outcome is never
    blocked — declining a weak proposal must always be possible regardless of
    how many reviews came in.
    """
    if call_step is None or outcome == WorkflowStepOutcomes.DECLINED:
        return

    # Checklist gate: a step with an attached, required checklist cannot be
    # completed until every required question is answered (WAL-9484). Advisory
    # checklists (checklist_required=False) never block.
    if call_step.checklist_id and call_step.checklist_required:
        completion = proposal.get_checklist_completion_for(call_step.checklist)
        if completion is None:
            unanswered = call_step.checklist.questions.filter(required=True)
        else:
            unanswered = completion.get_unanswered_required_questions()
        if unanswered.exists():
            raise ValueError(
                f"Cannot complete {_step_label(current_instance.step)}: "
                f"{unanswered.count()} required checklist "
                f"{'question is' if unanswered.count() == 1 else 'questions are'} "
                "unanswered."
            )

    if call_step.min_reviewers is None and call_step.min_score_threshold is None:
        return

    submitted = proposal.review_set.filter(state=models.Review.States.SUBMITTED)
    if (
        call_step.min_reviewers is not None
        and submitted.count() < call_step.min_reviewers
    ):
        raise ValueError(
            f"Cannot complete {current_instance.step}: "
            f"{submitted.count()} of {call_step.min_reviewers} required reviews "
            "have been submitted."
        )
    if call_step.min_score_threshold is not None:
        avg_score = submitted.aggregate(avg=Avg("summary_score"))["avg"]
        if avg_score is None or avg_score < call_step.min_score_threshold:
            raise ValueError(
                f"Cannot complete {current_instance.step}: the average review "
                f"score ({avg_score if avg_score is not None else 0}) is below "
                f"the required minimum of {call_step.min_score_threshold}."
            )


def _enforce_award_amounts(proposal, current_instance, outcome):
    """Block approving an award that grants an offering no amount.

    The award editor refuses such an item, but one can still be there: copied
    from a request written before amounts were required, or seeded around the
    API. Approving it would provision a resource with no quota. The rule is
    the one a proposal is submitted under. Declining is never blocked.
    """
    if (
        current_instance.step != DECISION_STEP
        or outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES
    ):
        return
    # A proposal that reached the step without its activation has no award
    # yet; allocation would copy one from the request, so check that copy.
    utils.prefill_awarded_resources(proposal)
    missing = proposal.offerings_missing_awarded_amounts()
    if missing:
        raise ValueError(
            f"Cannot complete {_step_label(current_instance.step)}: awarded "
            f"amounts are missing for the following offerings: "
            f"{', '.join(missing)}."
        )


def _advance_to_next(proposal, from_step, acting_user=None):
    """Advance the proposal past ``from_step`` to the next enabled step.

    Returns the newly active step instance, or None if the workflow
    terminated (proposal accepted). Reaching the terminal step both accepts
    the proposal *and* provisions it (project + resources), so the workflow
    engine and the legacy ``approve`` action converge on the same side
    effects. ``allocate_proposal`` is not idempotent — it always creates a
    Project — so allocation is guarded on the proposal not already having one.
    ``approved_by`` is stamped unconditionally (not only inside the allocation
    branch) so acceptance always records who decided it, even when a project
    already exists and allocation is skipped.
    """
    next_step_def = _next_enabled_step(proposal, from_step)
    if next_step_def is None:
        approver = acting_user or get_system_robot()
        if proposal.project_id is None:
            utils.allocate_proposal(proposal, approved_by=approver)
        proposal.approved_by = approver
        proposal.state = ProposalStates.ACCEPTED
        proposal.workflow_step = None
        proposal.save(update_fields=["state", "workflow_step", "approved_by"])
        return None

    proposal.workflow_step = next_step_def.id
    proposal.save(update_fields=["workflow_step"])
    return _activate_next_step(proposal, next_step_def)


def is_awaiting_manual_advance(proposal) -> bool:
    """True if the proposal's current step is COMPLETED with transition_mode=manual.

    ProposalViewSet.get_queryset replicates this predicate as queryset
    annotations to avoid an N+1 on list; keep the two in sync.
    """
    if not proposal.workflow_step or proposal.decision_held:
        return False
    instance = (
        proposal.workflow_step_instances.filter(step=proposal.workflow_step)
        .order_by("-created")
        .first()
    )
    if not instance or instance.status != WorkflowStepInstanceStatuses.COMPLETED:
        return False
    call_step = models.CallWorkflowStep.objects.filter(
        call=proposal.round.call, step=proposal.workflow_step
    ).first()
    return bool(call_step and call_step.transition_mode == TransitionModes.MANUAL)


@transaction.atomic
def advance_step(proposal, acting_user=None):
    """Manually advance a workflow parked on a manual-mode completed step.

    Returns the newly active step instance, or None if the workflow
    terminated. Raises ValueError if the proposal is not in a state where
    manual advance is valid. ``acting_user`` is the call manager confirming the
    advance; it is recorded as ``approved_by`` if this advance reaches the
    terminal (allocation) step.
    """
    if proposal.decision_held:
        raise ValueError(
            "The decision is held until the round's results are published."
        )
    if not is_awaiting_manual_advance(proposal):
        raise ValueError("Workflow is not awaiting manual advance.")
    return _advance_to_next(proposal, proposal.workflow_step, acting_user=acting_user)


@transaction.atomic
def reject_at_step(proposal, current_instance, reason, completed_by, internal_notes=""):
    """Mark the active step as completed with rejection and reject the proposal.

    ``internal_notes`` is appended to any pre-existing internal notes on the
    instance; see ``complete_step`` for the rationale.
    """
    current_instance.status = WorkflowStepInstanceStatuses.COMPLETED
    current_instance.outcome = WorkflowStepOutcomes.REJECTED
    current_instance.outcome_reason = reason
    current_instance.completed_at = timezone.now()
    current_instance.completed_by = completed_by
    if internal_notes:
        current_instance.internal_notes = _merge_notes(
            current_instance.internal_notes, internal_notes
        )
    current_instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "internal_notes",
            "completed_at",
            "completed_by",
        ]
    )
    if _decision_is_withheld(proposal, current_instance.step):
        _hold_decision(proposal)
        return
    notification_rules.dispatch_step_event(
        current_instance, NotificationRuleTriggers.STEP_REJECTED
    )
    proposal.state = ProposalStates.REJECTED
    proposal.workflow_step = None
    proposal.save(update_fields=["state", "workflow_step"])


def _enter_parked_step(proposal):
    """Start the step a proposal parked on a completed step waits to enter.

    Only so that a rejection can be recorded on it: no start notification is
    sent, the step ends as it begins. Returns the started instance, or None
    when no step follows.
    """
    next_step_def = _next_enabled_step(proposal, proposal.workflow_step)
    if next_step_def is None:
        return None
    instance = proposal.workflow_step_instances.select_for_update().get(
        step=next_step_def.id
    )
    instance.status = WorkflowStepInstanceStatuses.ACTIVE
    instance.started_at = timezone.now()
    instance.save(update_fields=["status", "started_at"])
    proposal.workflow_step = next_step_def.id
    proposal.save(update_fields=["workflow_step"])
    return instance


UNDECIDED_AT_COMPLETION_REASON = "Not decided when the round was completed."


@transaction.atomic
def reject_undecided(proposal, completed_by):
    """Reject a proposal left without a decision when its round is completed.

    The rejection is recorded on the step the proposal stands in, as if the
    step's actor had rejected it, and sends that step's rejection
    notifications. A proposal parked on a completed step, waiting to be moved
    on by hand, stands in the step it was waiting to enter: that step starts
    and is rejected at once, the completed one keeps its outcome. A proposal
    whose workflow has not started yet is rejected outright. Returns the
    state the proposal was in. The caller holds the proposal's row lock, then
    the round's, inside a transaction, and the round's results are published,
    so nothing is held back.
    """
    previous_state = proposal.state
    instance = (
        proposal.workflow_step_instances.select_for_update()
        .filter(
            step=proposal.workflow_step,
            status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        .first()
        if proposal.workflow_step
        else None
    )
    if instance is None and proposal.workflow_step:
        instance = _enter_parked_step(proposal)
    if instance is not None:
        reject_at_step(proposal, instance, UNDECIDED_AT_COMPLETION_REASON, completed_by)
        if proposal.decision_held:
            raise ValueError("The round still holds its decisions back.")
    else:
        proposal.state = ProposalStates.REJECTED
        proposal.workflow_step = None
        proposal.save(update_fields=["state", "workflow_step"])

    event_logger.emit(
        "Proposal {proposal_name} was rejected by {actor}: it had no decision "
        "when its round was completed.",
        event_type=EventType.PROPOSAL_REJECTED_AT_ROUND_COMPLETION,
        event_context={
            "proposal": proposal,
            "actor": completed_by.full_name or completed_by.username,
            "rejected_at_step": instance.step if instance else None,
            "previous_state": previous_state,
        },
        scopes=[proposal.round.call.manager.customer],
    )
    return previous_state


@transaction.atomic
def expire_step(current_instance):
    """Mark an active step as expired and advance the workflow.

    If a next enabled step exists, activate it and return the new instance.
    If no next step exists (terminal expiry), reject the proposal and return None.
    Returns a tuple (next_instance, proposal_was_rejected).
    """
    proposal = current_instance.proposal
    current_instance.status = WorkflowStepInstanceStatuses.EXPIRED
    current_instance.outcome = WorkflowStepOutcomes.EXPIRED
    current_instance.completed_at = timezone.now()
    current_instance.save(update_fields=["status", "outcome", "completed_at"])
    if _decision_is_withheld(proposal, current_instance.step):
        # A lapsed decision is an outcome like any other: whether it ends the
        # workflow or moves on to the award response, the move and the
        # expiry notice both tell the applicant, so both wait for the round.
        _hold_decision(proposal)
        return None, False
    notification_rules.dispatch_step_event(
        current_instance, NotificationRuleTriggers.STEP_EXPIRED
    )
    return _continue_after_expiry(proposal, current_instance.step)


def _continue_after_expiry(proposal, step):
    """Move past a lapsed step: on to the next step, or reject at the end."""
    next_step_def = _next_enabled_step(proposal, step)
    if next_step_def is None:
        proposal.state = ProposalStates.REJECTED
        proposal.workflow_step = None
        proposal.save(update_fields=["state", "workflow_step"])
        return None, True

    proposal.workflow_step = next_step_def.id
    proposal.save(update_fields=["workflow_step"])
    return _activate_next_step(proposal, next_step_def), False


def release_held_decision(proposal):
    """Carry out a held allocation decision as if it had just been made.

    Negative outcomes (declined, rejected) reject the proposal; a lapse goes
    where an unheld lapse goes -- the next step, or a rejection at the end;
    an approval continues to the next step (``award_response``) or, at the
    end of the workflow, accepts and provisions it. Every notification the
    decision would have sent -- the expiry notice included -- is sent here.
    Publication is the call manager's confirmation, so a manual transition
    mode does not park it again. Returns the proposal's new state when the
    workflow ended, None when it continues to another step. The caller holds
    the proposal's row lock inside a transaction.
    """
    instance = proposal.workflow_step_instances.get(step=DECISION_STEP)
    proposal.decision_held = False
    if instance.outcome == WorkflowStepOutcomes.EXPIRED:
        proposal.save(update_fields=["decision_held"])
        notification_rules.dispatch_step_event(
            instance, NotificationRuleTriggers.STEP_EXPIRED
        )
        next_instance, _ = _continue_after_expiry(proposal, DECISION_STEP)
        return proposal.state if next_instance is None else None
    if instance.outcome in (
        WorkflowStepOutcomes.NEGATIVE_OUTCOMES | {WorkflowStepOutcomes.REJECTED}
    ):
        notification_rules.dispatch_step_event(
            instance, NotificationRuleTriggers.STEP_REJECTED
        )
        proposal.state = ProposalStates.REJECTED
        proposal.workflow_step = None
        proposal.save(update_fields=["decision_held", "state", "workflow_step"])
        return proposal.state

    proposal.save(update_fields=["decision_held"])
    notification_rules.dispatch_step_event(
        instance, NotificationRuleTriggers.STEP_COMPLETED
    )
    next_instance = _advance_to_next(
        proposal, DECISION_STEP, acting_user=instance.completed_by
    )
    return proposal.state if next_instance is None else None


class DecisionNotReopenable(ValueError):
    """The proposal has no held decision that can still be taken back."""


@transaction.atomic
def reopen_held_decision(proposal, user, reason, deadline=None):
    """Take a held allocation decision back so it can be made again.

    Only while the round withholds its results: once they are published the
    decision has been (or is being) announced. The decision step becomes
    active again with its recorded outcome cleared, and the proposal counts
    as undecided for the round's publication. The deadline is ``deadline``
    when given; otherwise one that has already passed is dropped, so the
    step does not lapse again at the next sweep, and one still ahead is kept.
    Nothing is sent to anyone: the outcome was never announced. The reopen
    is logged on the proposal's feed without the reason or the outcome it
    undid: the applicant team reads that feed, and both would tell them the
    decision. Those two go to the server log only. Returns the reactivated
    step instance.

    Locks the proposal, then the round, the order every decision and the
    round's publication take.
    """
    proposal = (
        models.Proposal.objects.select_for_update(of=("self",))
        .select_related("round__call__manager__customer")
        .get(pk=proposal.pk)
    )
    if not proposal.decision_held or proposal.workflow_step != DECISION_STEP:
        raise DecisionNotReopenable("The proposal has no held decision to reopen.")
    call_round = models.Round.objects.select_for_update().get(pk=proposal.round_id)
    call_round.call = proposal.round.call
    if not call_round.withholds_results:
        raise DecisionNotReopenable(
            "The round's results are published; the decision can no longer be reopened."
        )
    instance = proposal.workflow_step_instances.select_for_update().get(
        step=DECISION_STEP
    )
    previous_outcome = instance.outcome

    instance.status = WorkflowStepInstanceStatuses.ACTIVE
    instance.outcome = None
    instance.outcome_reason = ""
    instance.completed_at = None
    instance.completed_by = None
    if deadline is not None:
        instance.deadline = deadline
    elif instance.deadline is not None and instance.deadline <= timezone.now():
        instance.deadline = None
    instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "completed_at",
            "completed_by",
            "deadline",
        ]
    )
    proposal.decision_held = False
    proposal.save(update_fields=["decision_held"])

    # Filed on the proposal's feed alone. Its scope guard keeps evaluators out
    # but lets the applicant team in, so the event names neither the outcome
    # taken back nor the free-text reason, which may well state it. The
    # managing organisation's feed is no place for it either: its owners need
    # not manage the call, and an applicant may be one of them.
    event_logger.emit(
        "Allocation decision on proposal {proposal_name} was reopened by {actor}.",
        event_type=EventType.PROPOSAL_DECISION_REOPENED,
        event_context={
            "proposal": proposal,
            "actor": user.full_name or user.username,
            "deadline": instance.deadline.isoformat() if instance.deadline else None,
        },
        scopes=[proposal],
    )
    logger.info(
        "Held decision on proposal %s reopened by user %s; previous outcome %s; "
        "reason: %s",
        proposal.uuid.hex,
        user.uuid.hex,
        previous_outcome,
        reason,
    )
    return instance
