"""Workflow step transition helpers.

Centralises the logic that advances a proposal's workflow when a step is
completed, rejected, or expires. Callers must hold a row-level lock on the
active step instance (via select_for_update) before invoking these helpers.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    WORKFLOW_STEPS,
    ProposalStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)


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
def complete_step(proposal, current_instance, outcome, outcome_reason, completed_by):
    """Complete the active step and advance the workflow.

    Returns the newly active next step instance, or None if the workflow
    terminated (proposal accepted).
    """
    current_instance.status = WorkflowStepInstanceStatuses.COMPLETED
    current_instance.outcome = outcome
    current_instance.outcome_reason = outcome_reason or ""
    current_instance.completed_at = timezone.now()
    current_instance.completed_by = completed_by
    current_instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "completed_at",
            "completed_by",
        ]
    )

    next_step_def = _next_enabled_step(proposal, current_instance.step)
    if next_step_def is None:
        proposal.state = ProposalStates.ACCEPTED
        proposal.workflow_step = None
        proposal.save(update_fields=["state", "workflow_step"])
        return None

    proposal.workflow_step = next_step_def.id
    proposal.save(update_fields=["workflow_step"])
    return _activate_next_step(proposal, next_step_def)


@transaction.atomic
def reject_at_step(proposal, current_instance, reason, completed_by):
    """Mark the active step as completed with rejection and reject the proposal."""
    current_instance.status = WorkflowStepInstanceStatuses.COMPLETED
    current_instance.outcome = WorkflowStepOutcomes.REJECTED
    current_instance.outcome_reason = reason
    current_instance.completed_at = timezone.now()
    current_instance.completed_by = completed_by
    current_instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
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
