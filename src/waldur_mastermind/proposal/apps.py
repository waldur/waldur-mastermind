from django.apps import AppConfig
from django.db.models import signals


class ProposalConfig(AppConfig):
    name = "waldur_mastermind.proposal"
    verbose_name = "Proposal"

    def ready(self):
        from waldur_core.permissions.utils import register_expiration_guard

        from . import handlers, models

        # Submitted-proposal team roles must not be auto-revoked on expiration.
        register_expiration_guard(handlers.is_submitted_proposal_role)

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
            handlers.seed_workflow_steps,
            sender=models.Call,
            dispatch_uid="waldur_mastermind.proposal.seed_workflow_steps",
        )
        signals.post_save.connect(
            handlers.seed_proposal_field_config,
            sender=models.Call,
            dispatch_uid="waldur_mastermind.proposal.seed_proposal_field_config",
        )
