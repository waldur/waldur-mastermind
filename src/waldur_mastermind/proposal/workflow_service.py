"""Workflow step transition helpers.

Centralises the logic that advances a proposal's workflow when a step is
completed, rejected, or expires. Callers must hold a row-level lock on the
active step instance (via select_for_update) before invoking these helpers.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Avg
from django.utils import timezone

from waldur_core.core.utils import get_system_robot
from waldur_mastermind.proposal import models, utils
from waldur_mastermind.proposal.enums import (
    WORKFLOW_STEPS,
    ProposalStates,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)


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
    instance.save(update_fields=["status", "started_at", "deadline"])
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
    if not proposal.workflow_step:
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
    proposal.state = ProposalStates.REJECTED
    proposal.workflow_step = None
    proposal.save(update_fields=["state", "workflow_step"])


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

    next_step_def = _next_enabled_step(proposal, current_instance.step)
    if next_step_def is None:
        proposal.state = ProposalStates.REJECTED
        proposal.workflow_step = None
        proposal.save(update_fields=["state", "workflow_step"])
        return None, True

    proposal.workflow_step = next_step_def.id
    proposal.save(update_fields=["workflow_step"])
    return _activate_next_step(proposal, next_step_def), False
