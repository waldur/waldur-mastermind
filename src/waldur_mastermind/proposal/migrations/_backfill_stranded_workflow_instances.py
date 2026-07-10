"""Backfill workflow instances for "stranded" legacy proposals.

Lives in a non-numeric module so it can be imported directly by tests (mirrors
marketplace/migrations/_enable_posix_account_backfill.py).

Stranded = a non-terminal proposal (submitted / in_review) with zero
ProposalWorkflowStepInstance rows. This can only be historical data:
  - proposals created before the workflow-step engine existed, and
  - proposals submitted while their call had no enabled steps.
New data can no longer strand: proposal creation requires an ACTIVE call and
activation now requires >= 1 enabled step (incl. the mandatory
allocation_decision), so submit always activates a first step.

The logic mirrors ProposalViewSet.submit's instantiation block and, for a call
that has no enabled steps configured, seeds (and force-enables) the default
call-manager-owned steps the same way handlers.seed_workflow_steps does — so no
proposal is left without an active step. Idempotent: only touches proposals with
zero instances and uses get_or_create per (proposal, step).
"""

from datetime import timedelta

from django.utils import timezone

from waldur_mastermind.proposal import enums


def backfill_stranded_workflow_instances(apps, schema_editor):
    Proposal = apps.get_model("proposal", "Proposal")
    CallWorkflowStep = apps.get_model("proposal", "CallWorkflowStep")
    ProposalWorkflowStepInstance = apps.get_model(
        "proposal", "ProposalWorkflowStepInstance"
    )

    PENDING = enums.WorkflowStepInstanceStatuses.PENDING
    SKIPPED = enums.WorkflowStepInstanceStatuses.SKIPPED
    ACTIVE = enums.WorkflowStepInstanceStatuses.ACTIVE
    CALL_MANAGER = enums.ResponsibleRoles.CALL_MANAGER

    stranded = (
        Proposal.objects.filter(
            state__in=[
                enums.ProposalStates.SUBMITTED,
                enums.ProposalStates.IN_REVIEW,
            ]
        )
        .filter(workflow_step_instances__isnull=True)
        .filter(round__isnull=False)
        .distinct()
    )

    for proposal in stranded.iterator():
        call = proposal.round.call

        # If the call has no enabled steps, seed and force-enable the default
        # call-manager-owned steps so the proposal can be activated (a call with
        # only disabled rows can't be repaired by get_or_create alone).
        if not CallWorkflowStep.objects.filter(call=call, is_enabled=True).exists():
            for step_def in enums.WORKFLOW_STEPS:
                if step_def.id == "award_response":
                    continue
                enabled = step_def.default_responsible_role == CALL_MANAGER
                step, created = CallWorkflowStep.objects.get_or_create(
                    call=call,
                    step=step_def.id,
                    defaults={"is_enabled": enabled},
                )
                if enabled and not created and not step.is_enabled:
                    step.is_enabled = True
                    step.save(update_fields=["is_enabled"])

        enabled_steps = list(
            CallWorkflowStep.objects.filter(call=call, is_enabled=True)
        )
        enabled_step_ids = {s.step for s in enabled_steps}
        first_step_id = next(
            (s.id for s in enums.WORKFLOW_STEPS if s.id in enabled_step_ids), None
        )

        for step_def in enums.WORKFLOW_STEPS:
            ProposalWorkflowStepInstance.objects.get_or_create(
                proposal=proposal,
                step=step_def.id,
                defaults={
                    "status": PENDING if step_def.id in enabled_step_ids else SKIPPED
                },
            )

        if first_step_id:
            first = ProposalWorkflowStepInstance.objects.get(
                proposal=proposal, step=first_step_id
            )
            first.status = ACTIVE
            first.started_at = timezone.now()
            call_step = next(
                (s for s in enabled_steps if s.step == first_step_id), None
            )
            if call_step and call_step.duration_in_days:
                first.deadline = first.started_at + timedelta(
                    days=call_step.duration_in_days
                )
            first.save(update_fields=["status", "started_at", "deadline"])
            proposal.state = enums.ProposalStates.IN_REVIEW
            proposal.workflow_step = first_step_id
            proposal.save(update_fields=["state", "workflow_step"])
