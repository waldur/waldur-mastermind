"""Signal handlers for proposal application."""

from django.contrib.contenttypes.models import ContentType

from waldur_core.checklist import models as checklist_models
from waldur_mastermind.proposal import enums, models


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
    """Pre-seed catalog workflow steps as enabled on call creation.

    Matches Figma F17: applicants should see the full evaluation tracker on
    any submitted proposal. Call managers can still disable individual steps
    from the call config UI; mandatory steps (e.g. allocation_decision) are
    protected separately and cannot be disabled.

    ``award_response`` is intentionally excluded: it is provisioned via
    ``allocation_decision.include_award_response`` and direct creation is
    blocked by the serializer.
    """
    if not created:
        return
    for step_def in enums.WORKFLOW_STEPS:
        if step_def.id == "award_response":
            continue
        models.CallWorkflowStep.objects.get_or_create(
            call=instance,
            step=step_def.id,
            defaults={"is_enabled": True},
        )
