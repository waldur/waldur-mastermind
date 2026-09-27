from django.apps import AppConfig
from django.db.models import signals


class ProposalConfig(AppConfig):
    name = "waldur_mastermind.proposal"
    verbose_name = "Proposal"

    def ready(self):
        from waldur_core.logging import event_dispatch
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.permissions.utils import register_expiration_guard

        from . import event_publishing, handlers, models

        permission_signals.role_revoked.connect(
            handlers.clear_panel_chair_on_role_revoked,
            dispatch_uid="waldur_mastermind.proposal.clear_panel_chair_on_role_revoked",
        )

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

        event_dispatch.register_event_chain(models.Call, event_publishing.call_chain)
        event_dispatch.register_event_chain(
            models.Proposal, event_publishing.proposal_chain
        )
        signals.post_save.connect(
            event_publishing.emit_call_state_changed,
            sender=models.Call,
            dispatch_uid="waldur_mastermind.proposal.emit_call_state_changed",
        )
        signals.post_save.connect(
            event_publishing.emit_proposal_state_changed,
            sender=models.Proposal,
            dispatch_uid="waldur_mastermind.proposal.emit_proposal_state_changed",
        )
