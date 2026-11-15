"""Which applicant details a viewer of a proposal reads.

Kept apart from ``serializers`` so that code outside the proposal app (the AI
assistant's proposal tools) can apply the same rule without importing the
serializers module, whose imports reach ``tasks`` and form a cycle when it is
loaded first from another app's ``ready()``.
"""

from waldur_core.permissions import enums as permissions_enums
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.fixtures import CallRole
from waldur_mastermind.proposal import models, proposal_roles
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.managers import (
    get_assigned_proposals,
    get_connected_calls,
    get_offering_manager_proposals,
    get_reviewed_proposals,
)


class ApplicantVisibility:
    """Whether one viewer sees proposals solely as an evaluator, and which
    applicant attributes the call's visibility config then conceals.

    Full-data viewers are staff, support, anyone holding a role on the
    proposal itself (the applicant team), and whoever
    manages the call -- a call manager, or an organiser through the managing
    organisation. Among the rest, evaluators are call reviewers, panel members,
    anyone holding a live review of the proposal (an accepted assignment
    grants no role), anyone sent an assignment to it they have not answered
    yet, the holders of an evaluator role on it, such as its
    rapporteurs, and the providers: the managers of an offering the proposal
    requested or was awarded, who assess it and later serve the award. They
    see only what the call exposes. Anyone else is not an evaluator and sees
    whatever their access otherwise gives them.

    Lookups are per viewer, per call or per proposal set and are memoised, so
    one instance can resolve a whole listing without a query per row.
    """

    def __init__(self, user):
        self.user = user
        self._evaluator_call_ids = None
        self._reviewed_proposal_ids = None
        self._assigned_proposal_ids = None
        self._held_proposal_ids = None
        self._provided_proposal_ids = None
        self._proposal_role_ids = None
        self._call_managed = {}
        self._concealed_by_call = {}

    def _evaluates(self, proposal) -> bool:
        if self._evaluator_call_ids is None:
            self._evaluator_call_ids = set(
                get_connected_calls(
                    self.user, [CallRole.REVIEWER, CallRole.PANEL_MEMBER]
                )
            )
        if proposal.round.call_id in self._evaluator_call_ids:
            return True
        if self._reviewed_proposal_ids is None:
            self._reviewed_proposal_ids = set(get_reviewed_proposals(self.user))
        if proposal.id in self._reviewed_proposal_ids:
            return True
        if self._assigned_proposal_ids is None:
            self._assigned_proposal_ids = set(get_assigned_proposals(self.user))
        if proposal.id in self._assigned_proposal_ids:
            return True
        if self._held_proposal_ids is None:
            self._held_proposal_ids = set(proposal_roles.proposals_held_by(self.user))
        if proposal.id in self._held_proposal_ids:
            return True
        if self._provided_proposal_ids is None:
            self._provided_proposal_ids = set(get_offering_manager_proposals(self.user))
        return proposal.id in self._provided_proposal_ids

    def _on_applicant_team(self, proposal) -> bool:
        # The current team, as ``proposal_roles.is_on_applicant_team`` reads
        # it: an author removed from the team is no longer the applicant.
        if self._proposal_role_ids is None:
            self._proposal_role_ids = set(
                proposal_roles.applicant_team_proposal_ids(self.user)
            )
        return proposal.id in self._proposal_role_ids

    def _manages_call(self, call) -> bool:
        if call.id not in self._call_managed:
            self._call_managed[call.id] = call.id in get_connected_calls(
                self.user, CallRole.MANAGER
            ) or permissions_utils.has_permission_on_any_source(
                self.user,
                permissions_enums.PermissionEnum.UPDATE_CALL,
                call,
                proposal_permissions.CALL_PERMISSION_SOURCES,
            )
        return self._call_managed[call.id]

    def is_reviewer_only(self, proposal) -> bool:
        user = self.user
        if not user or user.is_anonymous:
            return False
        if user.is_staff or user.is_support:
            return False
        if not self._evaluates(proposal):
            return False
        if self._on_applicant_team(proposal):
            return False
        return not self._manages_call(proposal.round.call)

    def concealed_attributes(self, proposal) -> set[str] | None:
        """None for a viewer who is not solely an evaluator of ``proposal``;
        otherwise the attributes its call's config conceals."""
        if not self.is_reviewer_only(proposal):
            return None
        call_id = proposal.round.call_id
        if call_id not in self._concealed_by_call:
            config_model = models.CallApplicantVisibilityConfig
            exposed = set(config_model.get_exposed_fields_for_call(proposal.round.call))
            self._concealed_by_call[call_id] = (
                set(config_model.get_attribute_names()) - exposed
            )
        return set(self._concealed_by_call[call_id])
