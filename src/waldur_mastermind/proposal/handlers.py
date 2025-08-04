"""Signal handlers for proposal application."""

from . import models


def create_checklist_completion(sender, instance, created, **kwargs):
    """Create checklist completion tracking when proposal is created."""
    if created and instance.round.call.compliance_checklist:
        models.ProposalChecklistCompletion.objects.create(
            proposal=instance, checklist=instance.round.call.compliance_checklist
        )
