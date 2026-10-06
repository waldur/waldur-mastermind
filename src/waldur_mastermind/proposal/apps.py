from django.apps import AppConfig
from django.db.models import signals


class ProposalConfig(AppConfig):
    name = "waldur_mastermind.proposal"
    verbose_name = "Proposal"

    def ready(self):
        from waldur_core.logging import event_dispatch
        from waldur_core.logging import utils as logging_utils
        from waldur_core.logging.enums import EventType
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.permissions.utils import (
            register_expiration_guard,
            register_role_change_guard,
            register_role_event_describer,
        )
        from waldur_core.users.utils import register_invitation_scope_overseer

        from . import event_publishing, handlers, models, permissions, serializers

        permission_signals.role_revoked.connect(
            handlers.clear_panel_chair_on_role_revoked,
            dispatch_uid="waldur_mastermind.proposal.clear_panel_chair_on_role_revoked",
        )

        register_role_change_guard(handlers.guard_proposal_team_change)
        register_invitation_scope_overseer(handlers.oversees_proposal_invitations)
        # A proposal never loses its last manager to expiry either.
        register_expiration_guard(handlers.is_last_proposal_manager_role)
        # Team changes are logged once, by the generic role event, which
        # this enriches with the proposal's state.
        register_role_event_describer(
            models.Proposal, handlers.describe_proposal_role_event
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
            handlers.seed_project_role_mappings,
            sender=models.Call,
            dispatch_uid="waldur_mastermind.proposal.seed_project_role_mappings",
        )
        signals.post_save.connect(
            handlers.seed_proposal_field_config,
            sender=models.Call,
            dispatch_uid="waldur_mastermind.proposal.seed_proposal_field_config",
        )

        # Evaluators read the proposal but not its team administration trail.
        logging_utils.register_scope_event_guard(
            models.Proposal, serializers.can_view_proposal_event_feed
        )
        # That a held decision was reopened tells that one had been made: only
        # those who may see held decisions learn it before the round publishes.
        logging_utils.register_event_type_guard(
            EventType.PROPOSAL_DECISION_REOPENED,
            permissions.proposals_with_held_decisions_hidden_from,
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
