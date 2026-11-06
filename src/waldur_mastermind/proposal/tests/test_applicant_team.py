"""The applicant team is the proposal's team as it stands.

Access goes to whoever holds a role on the proposal now, never through
``created_by``: an author removed from the team is nobody to the proposal.
Any current proposal manager acts for the applicant; the other members read.
The team settles once the award is accepted. Whoever joins it stops
evaluating the proposal, as for a recorded conflict of interest.
"""

from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CallRole, OfferingRole, ProposalRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models as users_models
from waldur_core.users.tests import factories as users_factories
from waldur_mastermind.proposal import (
    contributions,
    enums,
    managers,
    models,
    notification_rules,
    proposal_roles,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ContributionOutcomes,
    OutcomeSets,
    ProposalRoleEligibility,
    ProposalStates,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin
from waldur_mastermind.proposal.tests.test_step_contributions import (
    PerOfferingAssessmentBase,
)

MANAGER = RoleEnum.PROPOSAL_MANAGER
MEMBER = RoleEnum.PROPOSAL_MEMBER


def _proposal_url(proposal, action=None):
    return factories.ProposalFactory.get_url(proposal, action=action)


class TeamTestMixin(CallStepsTestMixin):
    """A proposal whose author manages it, with a second manager and a
    member, on a call whose first step after the check is the applicant's."""

    def setUp(self):
        super().setUp()
        for permission in (
            PermissionEnum.MANAGE_PROPOSAL,
            PermissionEnum.UPDATE_PROPOSAL,
            PermissionEnum.UPDATE_PROPOSAL_PERMISSION,
            PermissionEnum.DELETE_PROPOSAL_PERMISSION,
        ):
            ProposalRole.MANAGER.add_permission(permission)
        self.author = self.proposal.created_by
        self.co_manager = structure_factories.UserFactory()
        self.proposal.add_user(self.co_manager, ProposalRole.MANAGER)
        self.member = structure_factories.UserFactory()
        self.proposal.add_user(self.member, ProposalRole.MEMBER)
        self.staff = self.fixture.staff
        self.statement = self.add_step(
            "statement",
            StepKinds.APPLICANT_ACTION,
            "Statement",
            1,
            outcome_set=OutcomeSets.SUBMITTED,
            completer=ResponsibleRoles.APPLICANT,
        )

    def remove_author(self):
        self.proposal.remove_user(self.author, ProposalRole.MANAGER)

    def reach_statement(self):
        self.submit()
        response = self.complete("eligible")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "statement")

    def act(self, action, user, **payload):
        self.proposal.refresh_from_db()
        self.client.force_authenticate(user)
        return self.client.post(
            _proposal_url(self.proposal, action),
            {"step_uuid": self.proposal.current_step.uuid.hex, **payload},
        )


class ApplicantStepDraftTest(TeamTestMixin, test.APITestCase):
    def test_the_team_reads_its_own_draft_on_an_applicant_step(self):
        self.reach_statement()
        instance = self.proposal.current_step
        draft = contributions.resolve_contribution(self.co_manager, instance)
        self.assertIn(draft.status, StepContributionStatuses.OPEN)
        self.assertTrue(contributions.user_can_read(self.member, draft))
        self.assertTrue(contributions.user_can_read(self.author, draft))


class AuthorOffTheTeamTest(TeamTestMixin, test.APITestCase):
    def test_an_author_removed_from_the_team_loses_the_proposal(self):
        self.remove_author()
        self.assertFalse(
            proposal_roles.is_on_applicant_team(self.author, self.proposal)
        )
        self.assertNotIn(
            self.author.id, proposal_roles.applicant_team_user_ids(self.proposal)
        )
        self.client.force_authenticate(self.author)
        self.assertEqual(
            self.client.get(_proposal_url(self.proposal)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertNotIn(
            self.proposal.uuid.hex,
            [
                row["uuid"]
                for row in self.client.get(
                    factories.ProposalFactory.get_list_url(),
                    {"as_role": ResponsibleRoles.APPLICANT},
                ).data
            ],
        )

    def test_the_remaining_manager_submits_it(self):
        self.remove_author()
        self.client.force_authenticate(self.co_manager)
        response = self.client.post(_proposal_url(self.proposal, "submit"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_author_is_not_told_once_off_the_team(self):
        self.remove_author()
        users = notification_rules._applicant_users(self.proposal)
        self.assertNotIn(self.author, users)
        self.assertIn(self.member, users)


class WhoActsForTheApplicantTest(TeamTestMixin, test.APITestCase):
    def test_any_manager_completes_the_applicant_step(self):
        self.reach_statement()
        response = self.act(
            "complete_workflow_step", self.co_manager, outcome="submitted"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_member_reads_but_does_not_act(self):
        self.reach_statement()
        response = self.act("complete_workflow_step", self.member, outcome="submitted")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            proposal_permissions.user_can_read_step_checklist(
                self.member, self.proposal, self.statement
            )
        )
        self.assertFalse(
            proposal_permissions.user_holds_step_role(
                self.member, self.proposal, self.statement
            )
        )

    def test_the_author_off_the_team_no_longer_acts(self):
        self.reach_statement()
        self.remove_author()
        response = self.act("complete_workflow_step", self.author, outcome="submitted")
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertTrue(
            proposal_permissions.user_holds_step_role(
                self.co_manager, self.proposal, self.statement
            )
        )

    def test_the_applicant_cannot_reject_at_their_own_step(self):
        self.reach_statement()
        response = self.act("reject_workflow_step", self.author, reason="We withdraw.")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_staff_may_still_reject_there(self):
        self.reach_statement()
        response = self.act("reject_workflow_step", self.staff, reason="Withdrawn.")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class InternalNotesTest(TeamTestMixin, test.APITestCase):
    def test_the_whole_team_is_denied_internal_notes(self):
        # A member who also sits on the call's panel reads as an applicant,
        # even on the panel's own step, which another panel member reads.
        self.call.add_user(self.member, CallRole.PANEL_MEMBER)
        panel_step = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="panel_review",
            status=WorkflowStepInstanceStatuses.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        self.assertFalse(
            proposal_permissions.user_can_view_internal_notes(
                self.member, self.proposal, panel_step
            )
        )
        self.assertTrue(
            proposal_permissions.user_can_view_internal_notes(
                self.fixture.panel_member, self.proposal, panel_step
            )
        )

    def test_the_team_does_not_manage_a_held_decision(self):
        self.call.add_user(self.member, CallRole.MANAGER)
        self.assertFalse(
            proposal_permissions.user_can_manage_held_decision(
                self.member, self.proposal
            )
        )


class TeamSettledAfterAwardTest(TeamTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        self.outsider = structure_factories.UserFactory(email="late@example.com")

    def _post(self, action, user, role, as_user):
        self.client.force_authenticate(as_user)
        return self.client.post(
            _proposal_url(self.proposal, action),
            {"user": user.uuid.hex, "role": role},
        )

    def test_nobody_changes_the_team_once_accepted(self):
        for actor in (self.staff, self.fixture.call_manager, self.author):
            for action, user, role in (
                ("add_user", self.outsider, MEMBER),
                ("delete_user", self.member, MEMBER),
                ("update_user", self.member, MEMBER),
            ):
                response = self._post(action, user, role, actor)
                self.assertEqual(
                    response.status_code,
                    status.HTTP_400_BAD_REQUEST,
                    (actor, action, response.data),
                )
                self.assertIn("award is accepted", str(response.data))
        self.assertFalse(self.proposal.has_user(self.outsider))
        self.assertTrue(self.proposal.has_user(self.member, ProposalRole.MEMBER))

    def test_nobody_is_invited_once_accepted(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            users_factories.InvitationBaseFactory.get_list_url(),
            {
                "email": "late@example.com",
                "scope": _proposal_url(self.proposal),
                "role": ProposalRole.MEMBER.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_invitation_from_before_is_not_accepted(self):
        invitation = users_models.Invitation.objects.create(
            scope=self.proposal,
            customer=self.proposal.project.customer,
            role=ProposalRole.MEMBER,
            email=self.outsider.email,
            created_by=self.author,
        )
        self.client.force_authenticate(self.outsider)
        response = self.client.post(
            users_factories.InvitationBaseFactory.get_url(invitation, "accept")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.proposal.has_user(self.outsider))


class CallOrganiserActsLikeAManagerTest(TeamTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.organiser = self.fixture.call_organizer_user

    def test_the_organiser_completes_a_step(self):
        self.submit()
        self.assertTrue(
            proposal_permissions.user_can_act_on_active_step(
                self.organiser, self.proposal
            )
        )
        response = self.complete("eligible", user=self.organiser)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "statement")

    def test_the_organiser_does_not_act_for_the_applicant(self):
        self.reach_statement()
        response = self.act(
            "complete_workflow_step", self.organiser, outcome="submitted"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_organiser_completes_a_step_for_the_round(self):
        self.submit()
        self.client.force_authenticate(self.organiser)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.RoundFactory.get_url(
                    self.call, self.proposal.round, "complete_workflow_step"
                ),
                {"step": "administrative_check", "outcome": "eligible"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [row["proposal_uuid"] for row in response.data["completed"]],
            [self.proposal.uuid.hex],
        )


class StepLockedUnderProposalsTest(TeamTestMixin, test.APITestCase):
    def _patch(self, step, payload):
        self.client.force_authenticate(self.fixture.call_manager)
        return self.client.patch(
            f"http://testserver/api/proposal-protected-calls/{self.call.uuid.hex}"
            f"/workflow_steps/{step.uuid.hex}/",
            payload,
            format="json",
        )

    def test_completer_and_enablement_are_fixed_while_a_proposal_stands_on_it(self):
        self.reach_statement()
        for payload in (
            {"completer": ResponsibleRoles.CALL_MANAGER},
            {"is_enabled": False},
        ):
            response = self._patch(self.statement, payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)
            self.assertIn("Proposals stand on this step", str(response.data))
        self.statement.refresh_from_db()
        self.assertEqual(self.statement.completer, ResponsibleRoles.APPLICANT)
        self.assertTrue(self.statement.is_enabled)

    def test_they_change_while_no_proposal_stands_on_it(self):
        self.submit()
        response = self._patch(self.statement, {"is_enabled": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class StepFormLockedOnceReachedTest(TeamTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.form = self._form()
        self.statement.checklist = self.form
        self.statement.save(update_fields=["checklist"])

    def _form(self):
        return checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP
        )

    def _patch(self, payload):
        self.client.force_authenticate(self.fixture.call_manager)
        return self.client.patch(
            f"http://testserver/api/proposal-protected-calls/{self.call.uuid.hex}"
            f"/workflow_steps/{self.statement.uuid.hex}/",
            payload,
            format="json",
        )

    def test_the_form_is_kept_once_a_proposal_reached_the_step(self):
        self.reach_statement()
        for payload in ({"checklist": self._form().uuid.hex}, {"checklist": None}):
            response = self._patch(payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)
            self.assertIn("checklist", response.data)
        self.statement.refresh_from_db()
        self.assertEqual(self.statement.checklist, self.form)

    def test_the_form_is_kept_after_proposals_left_the_step(self):
        self.reach_statement()
        response = self.act("complete_workflow_step", self.author, outcome="submitted")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._patch({"checklist": None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_form_changes_while_proposals_have_yet_to_reach_the_step(self):
        self.submit()
        other = self._form()
        response = self._patch({"checklist": other.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.statement.refresh_from_db()
        self.assertEqual(self.statement.checklist, other)


class JoiningTheTeamEndsEvaluatingTest(TeamTestMixin, test.APITestCase):
    """An evaluator who joins the applicant team, by a grant or an accepted
    invitation, is treated as for a recorded conflict of interest."""

    def setUp(self):
        super().setUp()
        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.ANY_USER,
            max_holders=3,
        )
        self.submit()
        self.evaluator = structure_factories.UserFactory(email="eval@example.com")
        self.assignment = proposal_roles.assign(
            self.proposal, self.role, self.evaluator, assigned_by=self.staff
        )
        self.assertTrue(proposal_roles.holds_role(self.evaluator, self.proposal))

    def _join(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.proposal.add_user(
                self.evaluator, ProposalRole.MEMBER, created_by=self.staff
            )

    def test_their_evaluator_role_is_revoked(self):
        self._join()
        self.assignment.refresh_from_db()
        self.assertIsNotNone(self.assignment.revoked_at)
        self.assertEqual(
            self.assignment.revoked_reason, proposal_roles.JOINED_TEAM_REASON
        )
        self.assertEqual(self.assignment.revoked_reason, "Joined the applicant team.")
        self.assertFalse(proposal_roles.holds_role(self.evaluator, self.proposal))

    def test_an_accepted_invitation_revokes_it_too(self):
        invitation = users_models.Invitation.objects.create(
            scope=self.proposal,
            customer=self.proposal.project.customer,
            role=ProposalRole.MEMBER,
            email=self.evaluator.email,
            created_by=self.staff,
        )
        self.client.force_authenticate(self.evaluator)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                users_factories.InvitationBaseFactory.get_url(invitation, "accept")
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            proposal_roles.is_on_applicant_team(self.evaluator, self.proposal)
        )
        self.assignment.refresh_from_db()
        self.assertEqual(
            self.assignment.revoked_reason, proposal_roles.JOINED_TEAM_REASON
        )

    def test_the_role_stops_even_without_the_revocation(self):
        # A role granted past the signal: the access checks hold on their own.
        UserRole.objects.create(
            user=self.evaluator,
            role=ProposalRole.MEMBER,
            scope=self.proposal,
        )
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.revoked_at)
        self.assertFalse(proposal_roles.holds_role(self.evaluator, self.proposal))
        self.assertNotIn(
            self.proposal.id, set(proposal_roles.proposals_held_by(self.evaluator))
        )
        self.assertFalse(
            proposal_permissions.user_can_view_proposal_roles(
                self.evaluator, self.proposal
            )
        )

    def test_their_review_can_no_longer_be_submitted(self):
        reviewer = self.evaluator
        review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=reviewer,
            state=models.Review.States.IN_REVIEW,
        )
        self.assertTrue(managers.holds_live_review(reviewer, self.proposal))
        self._join()
        review.refresh_from_db()
        self.assertEqual(review.state, models.Review.States.REJECTED)
        self.assertEqual(review.rejection_cause, enums.ReviewRejectionCauses.WITHDRAWN)
        self.assertFalse(managers.holds_live_review(reviewer, self.proposal))

        # Even put back in review, it is not theirs to submit any more.
        models.Review.objects.filter(pk=review.pk).update(
            state=models.Review.States.IN_REVIEW
        )
        self.assertFalse(managers.holds_live_review(reviewer, self.proposal))
        self.client.force_authenticate(reviewer)
        response = self.client.post(
            factories.ReviewFactory.get_url(review, action="submit"),
            {"summary_score": 5},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_their_unanswered_assignments_are_blocked(self):
        profile = factories.ReviewerProfileFactory(user=self.evaluator)
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=entry,
            status=AssignmentBatchStatuses.SENT,
        )
        item = factories.AssignmentItemFactory(
            batch=batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.PENDING,
        )
        self._join()
        item.refresh_from_db()
        self.assertEqual(item.status, AssignmentItemStatuses.COI_BLOCKED)

    def test_their_open_contributions_are_withdrawn(self):
        instance = self.proposal.current_step
        contribution = models.StepContribution.objects.create(
            proposal_step=instance,
            contributor=self.evaluator,
            status=StepContributionStatuses.PENDING,
            due_at=timezone.now(),
        )
        self._join()
        contribution.refresh_from_db()
        self.assertEqual(contribution.status, StepContributionStatuses.WITHDRAWN)


class OfferingManagerJoiningTheTeamTest(PerOfferingAssessmentBase):
    """A centre admin who joins the applicant team no longer assesses it."""

    def test_they_no_longer_fill_their_offerings_slot(self):
        slot = self._slot(self.requested_a)
        self.assertTrue(contributions.user_can_fill(self.manager_a, slot))
        self.proposal.add_user(self.manager_a, ProposalRole.MEMBER)

        self.assertFalse(contributions.user_can_fill(self.manager_a, slot))
        self.assertFalse(contributions.may_contribute(self.manager_a, self.instance))
        self.assertEqual(
            self._answer(self.manager_a, slot).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self._submit(
                self.manager_a, slot, ContributionOutcomes.ACCEPTED
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        # The slot stays open for the rest of the offering's team.
        slot.refresh_from_db()
        self.assertIn(slot.status, StepContributionStatuses.OPEN)
        colleague = structure_factories.UserFactory()
        self.offering_a.add_user(colleague, OfferingRole.MANAGER)
        self.assertTrue(contributions.user_can_fill(colleague, slot))

    def test_the_team_reads_an_applicant_visible_assessment(self):
        member = structure_factories.UserFactory()
        self.proposal.add_user(member, ProposalRole.MEMBER)
        self.call_step.applicant_visible = True
        self.call_step.save(update_fields=["applicant_visible"])
        slot = self._slot(self.requested_a)
        # Still being written: not the evaluators' word yet.
        self.assertFalse(contributions.user_can_read(member, slot))
        slot.status = StepContributionStatuses.SUBMITTED
        slot.save(update_fields=["status"])
        call = self.proposal.round.call
        call.reviews_visible_to_submitters = True
        call.save(update_fields=["reviews_visible_to_submitters"])
        self.assertTrue(contributions.user_can_read(member, slot))
        self.assertTrue(contributions.user_can_read(self.proposal.created_by, slot))

        # The author off the team reads nothing of it.
        author = self.proposal.created_by
        self.proposal.add_user(member, ProposalRole.MANAGER)
        self.proposal.remove_user(author, ProposalRole.MANAGER)
        self.assertFalse(contributions.user_can_read(author, slot))
