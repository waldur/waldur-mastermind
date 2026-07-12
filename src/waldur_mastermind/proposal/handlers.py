"""Signal handlers for proposal application."""

from django.contrib.contenttypes.models import ContentType

from waldur_core.checklist import models as checklist_models
from waldur_mastermind.proposal import enums, models


def is_submitted_proposal_role(user_role) -> bool:
    """Expiration-sweep guard (waldur_core.permissions.check_expired_permissions).

    A submitted proposal's team is part of the application record and feeds the
    awarded project's roles, so its grants must not be auto-revoked when their
    expiration_time passes. Draft proposals keep normal expiry, so a temporary
    drafting collaborator can still auto-lapse before submission.
    """
    scope = user_role.scope
    return (
        isinstance(scope, models.Proposal) and scope.state != enums.ProposalStates.DRAFT
    )


def create_checklist_completion(sender, instance, created, **kwargs):
    """Create checklist completion tracking when proposal is created."""
    if created and instance.round.call.compliance_checklist:
        proposal_content_type = ContentType.objects.get_for_model(instance)
        checklist_models.ChecklistCompletion.objects.create(
            scope_content_type=proposal_content_type,
            scope_object_id=instance.id,
            checklist=instance.round.call.compliance_checklist,
        )


def delete_checklist_completion(sender, instance, **kwargs):
    """Remove checklist completion tracking when proposal is deleted."""
    proposal_content_type = ContentType.objects.get_for_model(instance)
    checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=proposal_content_type,
        scope_object_id=instance.id,
    ).delete()


def seed_workflow_steps(sender, instance, created, **kwargs):
    """Seed catalog workflow steps on call creation.

    Only steps that have a working completion surface today — the
    call-manager-owned steps (administrative check, allocation decision) — are
    enabled by default, so a newly created call's workflow is fully drivable by
    the call manager end to end and needs no legacy Accept/Reject fallback. The
    remaining evaluation steps (offering-manager / reviewer / panel-member
    owned) are seeded *disabled*; a call manager can opt them in from the call
    config UI once their actor-facing surfaces exist, without stranding the
    workflow in the meantime.

    ``award_response`` is intentionally excluded: it is provisioned via
    ``allocation_decision.include_award_response`` and direct creation is
    blocked by the serializer. Mandatory steps (allocation_decision) cannot be
    disabled and are always enabled here.
    """
    if not created:
        return
    for step_def in enums.WORKFLOW_STEPS:
        if step_def.id == "award_response":
            continue
        enabled = (
            step_def.default_responsible_role == enums.ResponsibleRoles.CALL_MANAGER
        )
        models.CallWorkflowStep.objects.get_or_create(
            call=instance,
            step=step_def.id,
            defaults={"is_enabled": enabled},
        )
