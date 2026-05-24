from django.apps import AppConfig
from django.db.models import signals


class ProposalConfig(AppConfig):
    name = "waldur_mastermind.proposal"
    verbose_name = "Proposal"

    def ready(self):
        from . import handlers, models

        # Register signal handlers
        signals.post_save.connect(
            handlers.create_checklist_completion,
            sender=models.Proposal,
            dispatch_uid="waldur_mastermind.proposal.create_checklist_completion",
        )
        signals.pre_delete.connect(
            handlers.delete_checklist_completion,
            sender=models.Proposal,
            dispatch_uid="waldur_mastermind.proposal.delete_checklist_completion",
        )
        signals.post_save.connect(
            handlers.seed_mandatory_workflow_steps,
            sender=models.Call,
            dispatch_uid="waldur_mastermind.proposal.seed_mandatory_workflow_steps",
        )
