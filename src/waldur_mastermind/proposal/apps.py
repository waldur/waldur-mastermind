from django.apps import AppConfig
from django.db.models import signals


class ProposalConfig(AppConfig):
    name = "waldur_mastermind.proposal"
    verbose_name = "Proposal"

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.logging import event_dispatch
        from waldur_core.logging import utils as logging_utils
        from waldur_core.logging.enums import EventType
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.permissions.utils import (
            register_expiration_guard,
            register_role_change_guard,
            register_role_event_describer,
        )
        from waldur_core.structure import serializers as structure_serializers
        from waldur_core.structure.managers import (
            register_read_only_project_provider,
            register_read_only_resource_provider,
        )
        from waldur_core.users.utils import register_invitation_scope_overseer
        from waldur_mastermind.marketplace import dependents as marketplace_dependents

        from . import (
            activity,
            event_publishing,
            handlers,
            managers,
            models,
            offering_impact,
            order_link,
            permissions,
            project_link,
            reviewer_membership,
            step_inputs,
            utils,
        )

        # Calls are asked before the marketplace pulls an offering, a plan or
        # a component from under them, and follow a component's renaming.
        marketplace_dependents.register_provider(offering_impact.provider)
        marketplace_dependents.register_component_renamer(
            offering_impact.rename_component
        )

        # Which award an order provisions.
        from waldur_mastermind.marketplace import (
            serializers as marketplace_serializers,
        )

        core_signals.pre_serializer_fields.connect(
            sender=marketplace_serializers.OrderDetailsSerializer,
            receiver=order_link.add_award,
            dispatch_uid="waldur_mastermind.proposal.add_order_award",
        )
        marketplace_serializers.OrderDetailsSerializer.page_loaders.append(
            order_link.attach_awards
        )

        # Where a project awarded through a call came from.
        core_signals.pre_serializer_fields.connect(
            sender=structure_serializers.ProjectSerializer,
            receiver=project_link.add_awarded_proposal,
            dispatch_uid="waldur_mastermind.proposal.add_awarded_proposal",
        )

        permission_signals.role_revoked.connect(
            handlers.clear_panel_chair_on_role_revoked,
            dispatch_uid="waldur_mastermind.proposal.clear_panel_chair_on_role_revoked",
        )

        permission_signals.role_revoked.connect(
            handlers.settle_slots_on_call_role_revoked,
            dispatch_uid="waldur_mastermind.proposal.settle_slots_on_call_role_revoked",
        )

        # After the panel chair handler, which takes them off the panels.
        permission_signals.role_revoked.connect(
            handlers.end_evaluator_roles_on_call_role_revoked,
            dispatch_uid=(
                "waldur_mastermind.proposal.end_evaluator_roles_on_call_role_revoked"
            ),
        )

        signals.m2m_changed.connect(
            handlers.end_evaluator_roles_on_leaving_panel,
            sender=models.CallPanel.members.through,
            dispatch_uid="waldur_mastermind.proposal.end_evaluator_roles_on_leaving_panel",
        )

        signals.pre_save.connect(
            handlers.remember_panel_chair,
            sender=models.CallPanel,
            dispatch_uid="waldur_mastermind.proposal.remember_panel_chair",
        )

        signals.post_save.connect(
            handlers.end_evaluator_roles_on_chair_change,
            sender=models.CallPanel,
            dispatch_uid="waldur_mastermind.proposal.end_evaluator_roles_on_chair_change",
        )

        # One list of a call's reviewers: the pool and the reviewer role
        # are kept in step (see reviewer_membership).
        permission_signals.role_granted.connect(
            reviewer_membership.join_pool_on_role_grant,
            dispatch_uid="waldur_mastermind.proposal.join_pool_on_role_grant",
        )
        permission_signals.role_revoked.connect(
            reviewer_membership.leave_pool_on_role_revoke,
            dispatch_uid="waldur_mastermind.proposal.leave_pool_on_role_revoke",
        )
        signals.post_save.connect(
            reviewer_membership.grant_role_on_acceptance,
            sender=models.CallReviewerPool,
            dispatch_uid="waldur_mastermind.proposal.grant_role_on_acceptance",
        )

        permission_signals.role_granted.connect(
            handlers.end_evaluation_on_joining_team,
            dispatch_uid="waldur_mastermind.proposal.end_evaluation_on_joining_team",
        )

        register_role_change_guard(handlers.guard_proposal_team_change)
        register_role_change_guard(handlers.guard_call_reviewer_grant)
        # A call manager reads the projects the call allocated, and in them
        # only the resources the allocation provisioned.
        register_read_only_project_provider(managers.get_call_managed_project_ids)
        register_read_only_resource_provider(managers.get_call_allocated_resource_ids)
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
            handlers.attach_review_contribution,
            sender=models.Review,
            dispatch_uid="waldur_mastermind.proposal.attach_review_contribution",
        )
        signals.post_save.connect(
            handlers.attach_assignment_contribution,
            sender=models.AssignmentItem,
            dispatch_uid="waldur_mastermind.proposal.attach_assignment_contribution",
        )
        signals.post_save.connect(
            handlers.settle_slots_on_conflict,
            sender=models.ConflictOfInterest,
            dispatch_uid="waldur_mastermind.proposal.settle_slots_on_conflict",
        )
        signals.pre_delete.connect(
            handlers.delete_contribution_completions,
            sender=models.StepContribution,
            dispatch_uid="waldur_mastermind.proposal.delete_contribution_completions",
        )
        for model in (models.CallOfferingComponent, models.CallResourcePool):
            for signal in (signals.post_save, signals.post_delete):
                signal.connect(
                    handlers.notify_provider_about_changed_terms,
                    sender=model,
                    dispatch_uid=(
                        "waldur_mastermind.proposal.notify_provider_about_changed_terms"
                        f"_{model.__name__}_{id(signal)}"
                    ),
                )
        signals.post_save.connect(
            handlers.notify_provider_about_offering_request,
            sender=models.RequestedOffering,
            dispatch_uid="waldur_mastermind.proposal.notify_provider_about_offering_request",
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

        # Who reads which part of a proposal's history: the call team all of
        # it, the applicant team what it is told, evaluators what they did.
        logging_utils.register_scope_event_filter(
            models.Proposal, activity.proposal_event_rows
        )
        # A review's history is the reviewer's and the call team's.
        logging_utils.register_scope_event_guard(
            models.Review, activity.can_view_review_event_feed
        )
        # That a held decision was reopened tells that one had been made: only
        # those who may see held decisions learn it before the round publishes.
        logging_utils.register_event_type_guard(
            EventType.PROPOSAL_DECISION_REOPENED,
            permissions.proposals_with_held_decisions_hidden_from,
        )
        # As is that a decision is held at all.
        logging_utils.register_event_type_guard(
            EventType.PROPOSAL_DECISION_HELD,
            permissions.proposals_with_held_decisions_hidden_from,
        )
        # A round's history is the call team's: filed on the call, which
        # others may see too.
        for event_type in utils.ROUND_EVENT_TYPES:
            logging_utils.register_event_type_guard(
                event_type, permissions.calls_hiding_round_events_from
            )
        # Who released a report to the applicant is for those overseeing
        # the call: the applicant team reads the report, not who released it.
        logging_utils.register_event_type_guard(
            EventType.PROPOSAL_REPORT_RELEASED,
            step_inputs.proposals_hiding_report_releases_from,
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
