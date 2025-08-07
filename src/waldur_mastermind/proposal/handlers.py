"""Signal handlers for proposal application."""

from django.contrib.contenttypes.models import ContentType

from waldur_core.checklist import models as checklist_models


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
