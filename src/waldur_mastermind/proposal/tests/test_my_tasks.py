"""The current user's open workflow work, listed across calls.

Each task appears for the people who may act on it and for nobody else,
leaves once done, and follows the rules that gate acting: the list never
offers what the proposal page would refuse.
"""

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, OfferingRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    contributions,
    models,
    my_tasks,
    permissions,
    proposal_roles,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    COIStatuses,
    ContributorSources,
    OutcomeSets,
    ProposalRoleEligibility,
    ProposalStates,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    StepContributionStatuses,
    StepKinds,
    TransitionModes,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures

URL = "http://testserver/api/proposal-my-tasks/"
Needs = my_tasks.Needs


class MyTasksBase(test.APITestCase):
    """A call whose proposals walk: administrative check, a technical
    assessment per requested offering, a rapporteur assignment, the
    rapporteurs' report, a panel discussion, a response to the reports, the
    allocation decision and the award response."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager
        self.panel_1 = self.fixture.panel_member
        self.panel_2 = self._panel_member()
        self.panel_3 = self._panel_member()

        steps = models.CallWorkflowStep.objects.filter(call=self.call)
        steps.filter(key__in=["expert_review", "panel_review"]).update(is_enabled=False)
        self.tech = steps.get(key="technical_assessment")
        self.tech.is_enabled = True
        self.tech.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
        self.tech.per_requested_offering = True
        self.tech.outcome_set = OutcomeSets.PER_OFFERING
        self.tech.save()

        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.CALL_PANEL_MEMBER,
            min_holders=1,
            max_holders=2,
            allows_lead=True,
        )
        self.assignment = self._add_step(
            "rapporteur-assignment",
            StepKinds.ASSIGNMENT,
            2,
            completer=ResponsibleRoles.CALL_MANAGER,
        )
        self.assignment.assigns_roles.set([self.role])
        self.report = self._add_step(
            "rapporteur-report",
            StepKinds.ASSESSMENT,
            3,
            contributor_source=f"proposal_role:{self.role.pk}",
            completer=ResponsibleRoles.CALL_MANAGER,
            outcome_set=OutcomeSets.DONE,
        )
        self.panel = self._add_step(
            "panel-discussion",
            StepKinds.DECISION,
            4,
            contributor_source="call_role:panel_member",
            completer=ResponsibleRoles.CALL_MANAGER,
        )
        self.response = self._add_step(
            "response-to-reports",
            StepKinds.APPLICANT_ACTION,
            5,
            completer=ResponsibleRoles.APPLICANT,
            outcome_set=OutcomeSets.DONE,
        )
        self.award = factories.CallWorkflowStepFactory(
            call=self.call, key="award_response", is_enabled=True
        )

        self.manager_a, self.offering_a = self._offering()
        self.manager_b, self.offering_b = self._offering()
        self.proposal = self._submitted_proposal()

    # --- setup -------------------------------------------------------------

    def _panel_member(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.PANEL_MEMBER)
        return user

    def _add_step(self, key, kind, order, **values):
        models.CallWorkflowStep.make_room(self.call.pk, order)
        return models.CallWorkflowStep.objects.create(
            call=self.call, key=key, kind=kind, name=key.title(), order=order, **values
        )

    def _offering(self):
        manager = structure_factories.UserFactory()
        offering = marketplace_factories.OfferingFactory()
        offering.add_user(manager, OfferingRole.MANAGER)
        requested = factories.RequestedOfferingFactory(
            call=self.call, offering=offering, created_by=self.fixture.owner
        )
        return manager, requested

    def _submitted_proposal(self):
        proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        fixtures.author_as_manager(proposal)
        for requested in (self.offering_a, self.offering_b):
            factories.RequestedResourceFactory(
                proposal=proposal, requested_offering=requested
            )
        workflow_service.create_step_instances(proposal)
        workflow_service.activate_first_step(proposal)
        proposal.refresh_from_db()
        return proposal

    def move_to(self, key, proposal=None):
        """Complete the proposal's steps before ``key`` and start ``key``."""
        proposal = proposal or self.proposal
        target = proposal.workflow_step_instances.get(step=key)
        proposal.workflow_step_instances.filter(
            status=WorkflowStepInstanceStatuses.ACTIVE
        ).update(
            status=WorkflowStepInstanceStatuses.COMPLETED,
            completed_at=timezone.now(),
        )
        workflow_service._activate_next_step(proposal, target)
        proposal.current_step = target
        proposal.save(update_fields=["current_step"])
        target.refresh_from_db()
        return target

    def assign(self, user, is_lead=False, proposal=None):
        return proposal_roles.assign(
            proposal or self.proposal,
            self.role,
            user,
            assigned_by=self.call_manager,
            is_lead=is_lead,
        )

    # --- reading -----------------------------------------------------------

    def tasks(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(URL, {"page_size": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def needs(self, user):
        return [row["needed"] for row in self.tasks(user)]


class CentreAssessorTest(MyTasksBase):
    def setUp(self):
        super().setUp()
        self.instance = self.move_to("technical_assessment")

    def test_each_centre_sees_its_own_slot(self):
        for manager, requested in (
            (self.manager_a, self.offering_a),
            (self.manager_b, self.offering_b),
        ):
            rows = self.tasks(manager)
            self.assertEqual(len(rows), 1, rows)
            row = rows[0]
            self.assertEqual(row["needed"], Needs.CONTRIBUTE)
            self.assertEqual(row["detail"], requested.offering.name)
            self.assertEqual(row["proposal_uuid"], self.proposal.uuid.hex)
            self.assertEqual(row["proposal_slug"], self.proposal.slug)
            self.assertEqual(row["call_name"], self.call.name)
            self.assertEqual(row["round_name"], self.proposal.round.name)
            self.assertEqual(row["step_uuid"], self.instance.uuid.hex)
            self.assertEqual(row["step_kind"], StepKinds.ASSESSMENT)
            self.assertEqual(row["target"], my_tasks.Targets.PROPOSAL)
            self.assertEqual(
                row["target_section"], f"step-contributions-{self.tech.uuid.hex}"
            )
            # The page lets them fill it.
            slot = self.instance.contributions.get(requested_offering=requested)
            self.assertTrue(contributions.may_contribute(manager, self.instance))
            self.assertTrue(contributions.user_can_fill(manager, slot))

    def test_others_have_nothing_to_assess(self):
        for user in (
            self.panel_1,
            self.fixture.reviewer_1,
            self.proposal.created_by,
            structure_factories.UserFactory(),
        ):
            self.assertNotIn(Needs.CONTRIBUTE, self.needs(user))

    def test_submitted_slot_leaves_the_list(self):
        self.instance.contributions.filter(requested_offering=self.offering_a).update(
            status=StepContributionStatuses.SUBMITTED
        )
        self.assertEqual(self.needs(self.manager_a), [])
        self.assertEqual(self.needs(self.manager_b), [Needs.CONTRIBUTE])

    def test_assessor_on_the_applicant_team_sees_nothing(self):
        self.proposal.add_user(self.manager_a, ProposalRole.MEMBER)
        self.assertEqual(self.needs(self.manager_a), [])
        self.assertFalse(contributions.may_contribute(self.manager_a, self.instance))

    def test_closed_proposal_leaves_the_list(self):
        self.proposal.state = ProposalStates.CANCELED
        self.proposal.save()
        self.assertEqual(self.needs(self.manager_a), [])


class RapporteurTest(MyTasksBase):
    def setUp(self):
        super().setUp()
        self.move_to("rapporteur-assignment")
        self.lead = self.assign(self.panel_1, is_lead=True)
        self.second = self.assign(self.panel_2)

    def test_call_manager_assigns_evaluators(self):
        rows = self.tasks(self.call_manager)
        self.assertEqual([row["needed"] for row in rows], [Needs.ASSIGN])
        self.assertEqual(rows[0]["target_section"], "step-evaluators")
        self.assertTrue(
            permissions.user_can_act_on_active_step(self.call_manager, self.proposal)
        )

    def test_each_rapporteur_contributes_to_the_report(self):
        instance = self.move_to("rapporteur-report")
        for user in (self.panel_1, self.panel_2):
            self.assertEqual(self.needs(user), [Needs.CONTRIBUTE])
            self.assertTrue(contributions.may_contribute(user, instance))
        self.assertEqual(self.needs(self.panel_3), [])

    def test_only_the_lead_contributes_to_a_lead_only_report(self):
        self.report.contributors_lead_only = True
        self.report.save()
        instance = self.move_to("rapporteur-report")
        self.assertEqual(self.needs(self.panel_1), [Needs.CONTRIBUTE])
        self.assertEqual(self.needs(self.panel_2), [])
        self.assertFalse(contributions.may_contribute(self.panel_2, instance))

    def test_only_the_lead_completes_a_lead_only_report_of_their_role(self):
        self.report.contributors_lead_only = True
        self.report.completer = ResponsibleRoles.PROPOSAL_ROLE
        self.report.completer_proposal_role = self.role
        self.report.save()
        instance = self.move_to("rapporteur-report")
        instance.contributions.filter(contributor=self.panel_1).update(
            status=StepContributionStatuses.SUBMITTED
        )
        self.assertEqual(self.needs(self.panel_1), [Needs.COMPLETE])
        self.assertEqual(self.needs(self.panel_2), [])

    def test_call_manager_completes_the_report(self):
        self.move_to("rapporteur-report")
        self.assertEqual(self.needs(self.call_manager), [Needs.COMPLETE])

    def test_submitted_report_leaves_the_list(self):
        instance = self.move_to("rapporteur-report")
        instance.contributions.filter(contributor=self.panel_1).update(
            status=StepContributionStatuses.SUBMITTED
        )
        self.assertEqual(self.needs(self.panel_1), [])
        self.assertEqual(self.needs(self.panel_2), [Needs.CONTRIBUTE])

    def test_revoked_rapporteur_sees_nothing(self):
        instance = self.move_to("rapporteur-report")
        proposal_roles.revoke(self.second, self.call_manager)
        self.assertEqual(self.needs(self.panel_2), [])
        self.assertFalse(contributions.may_contribute(self.panel_2, instance))

    def test_rapporteur_with_a_conflict_sees_nothing(self):
        instance = self.move_to("rapporteur-report")
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.panel_1),
            proposal=self.proposal,
            status=COIStatuses.PENDING,
        )
        self.assertEqual(self.needs(self.panel_1), [])
        self.assertFalse(contributions.may_contribute(self.panel_1, instance))

    def test_rapporteur_confirms_no_conflict_first(self):
        models.ProposalRoleAssignment.objects.filter(pk=self.second.pk).update(
            coi_confirmation_required=True
        )
        self.move_to("rapporteur-report")
        rows = self.tasks(self.panel_2)
        self.assertEqual([row["needed"] for row in rows], [Needs.CONFIRM_NO_COI])
        self.assertEqual(rows[0]["target"], my_tasks.Targets.EVALUATOR_ROLES)
        self.assertEqual(rows[0]["target_uuid"], self.second.uuid.hex)
        self.assertEqual(rows[0]["detail"], "Rapporteur")
        self.assertNotIn(Needs.CONFIRM_NO_COI, self.needs(self.panel_1))

        models.ProposalRoleAssignment.objects.filter(pk=self.second.pk).update(
            coi_confirmed_at=timezone.now()
        )
        self.assertEqual(self.needs(self.panel_2), [Needs.CONTRIBUTE])


class RapporteursCompleteTest(MyTasksBase):
    """The rapporteurs complete their own report: once every contribution
    is in, the step waits for them, and both the task list and the
    proposal page say so."""

    def setUp(self):
        super().setUp()
        self.report.completer = ResponsibleRoles.PROPOSAL_ROLE
        self.report.completer_proposal_role = self.role
        self.report.save()
        self.move_to("rapporteur-assignment")
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)
        self.instance = self.move_to("rapporteur-report")

    def ready_to_complete(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, "workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = next(r for r in response.data if r["step"] == "rapporteur-report")
        return row["ready_to_complete"]

    def submit(self, user):
        self.instance.contributions.filter(contributor=user).update(
            status=StepContributionStatuses.SUBMITTED
        )

    def test_the_step_waits_on_the_contributions_first(self):
        self.submit(self.panel_1)
        self.assertEqual(self.needs(self.panel_1), [])
        self.assertEqual(self.needs(self.panel_2), [Needs.CONTRIBUTE])
        self.assertFalse(self.ready_to_complete(self.panel_1))

    def test_once_all_are_in_the_rapporteurs_complete_it(self):
        self.submit(self.panel_1)
        self.submit(self.panel_2)
        for user in (self.panel_1, self.panel_2):
            self.assertEqual(self.needs(user), [Needs.COMPLETE])
            self.assertTrue(self.ready_to_complete(user))
        # The call manager may complete any step of the call too.
        self.assertTrue(self.ready_to_complete(self.call_manager))
        for user in (self.panel_3, self.proposal.created_by):
            self.assertFalse(self.ready_to_complete(user))
        # Nothing completes it for them: completing may carry a decision.
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_a_gate_still_holding_keeps_it_unready(self):
        self.report.min_contributions = 3
        self.report.save()
        self.submit(self.panel_1)
        self.submit(self.panel_2)
        self.assertFalse(self.ready_to_complete(self.panel_1))


class UnassignedRoleTest(MyTasksBase):
    """A step whose evaluators nobody assigned: the step that assigns them
    was removed, or comes after it. The call's managers are asked to assign
    them, or the proposal would stand at the step with nobody told."""

    def test_call_manager_assigns_the_missing_evaluators(self):
        self.move_to("rapporteur-report")
        rows = self.tasks(self.call_manager)
        assign = [row for row in rows if row["needed"] == Needs.ASSIGN]
        self.assertEqual(len(assign), 1, rows)
        self.assertEqual(assign[0]["target_section"], "step-evaluators")
        self.assertEqual(assign[0]["detail"], "Rapporteur")
        self.assertIn(Needs.COMPLETE, [row["needed"] for row in rows])

    def test_nothing_to_assign_once_someone_holds_the_role(self):
        self.move_to("rapporteur-assignment")
        self.assign(self.panel_1, is_lead=True)
        self.move_to("rapporteur-report")
        self.assertNotIn(Needs.ASSIGN, self.needs(self.call_manager))

    def test_others_are_not_asked(self):
        self.move_to("rapporteur-report")
        for user in (self.panel_1, self.manager_a):
            self.assertNotIn(Needs.ASSIGN, self.needs(user))


class PanelTest(MyTasksBase):
    def test_panel_members_contribute_until_they_have(self):
        instance = self.move_to("panel-discussion")
        self.assertEqual(self.needs(self.panel_3), [Needs.CONTRIBUTE])
        self.assertTrue(contributions.may_contribute(self.panel_3, instance))
        factories.StepContributionFactory(
            proposal_step=instance,
            contributor=self.panel_3,
            status=StepContributionStatuses.SUBMITTED,
        )
        self.assertEqual(self.needs(self.panel_3), [])
        self.assertEqual(self.needs(self.call_manager), [Needs.DECIDE])


class CallManagerTest(MyTasksBase):
    def test_call_manager_completes_the_administrative_check(self):
        rows = self.tasks(self.call_manager)
        self.assertEqual([row["needed"] for row in rows], [Needs.DECIDE])
        self.assertEqual(rows[0]["step_name"], "Administrative check")
        self.assertTrue(
            permissions.user_can_act_on_active_step(self.call_manager, self.proposal)
        )
        for user in (self.panel_1, self.manager_a, self.proposal.created_by):
            self.assertEqual(self.needs(user), [])

    def test_completed_step_leaves_the_list(self):
        self.move_to("technical_assessment")
        self.assertEqual(self.needs(self.call_manager), [])

    def test_call_organiser_completes_it_too(self):
        organiser = self.fixture.call_organizer_user
        self.assertEqual(self.needs(organiser), [Needs.DECIDE])

    def test_call_manager_advances_a_parked_workflow(self):
        instance = self.proposal.current_step
        instance.call_step.transition_mode = TransitionModes.MANUAL
        instance.call_step.save()
        instance.status = WorkflowStepInstanceStatuses.COMPLETED
        instance.save()
        rows = self.tasks(self.call_manager)
        self.assertEqual([row["needed"] for row in rows], [Needs.ADVANCE])
        self.assertTrue(workflow_service.is_awaiting_manual_advance(self.proposal))

    def test_call_manager_releases_a_report(self):
        self.tech.release_to_applicant = True
        self.tech.save()
        self.move_to("technical_assessment")
        self.move_to("rapporteur-assignment")
        rows = self.tasks(self.call_manager)
        self.assertIn(Needs.RELEASE_REPORT, [row["needed"] for row in rows])
        release = next(r for r in rows if r["needed"] == Needs.RELEASE_REPORT)
        self.assertEqual(release["target_section"], "step-released-reports")
        self.assertNotIn(Needs.RELEASE_REPORT, self.needs(self.proposal.created_by))

        self.proposal.workflow_step_instances.filter(
            step="technical_assessment"
        ).update(released_at=timezone.now())
        self.assertNotIn(Needs.RELEASE_REPORT, self.needs(self.call_manager))


class CallManagerStandsInTest(MyTasksBase):
    """Work whose people are missing falls to the call's managers, who may
    act in their place, with the reason given."""

    def test_a_partition_without_a_team_is_assessed_by_the_call_manager(self):
        self.offering_b.offering.remove_user(self.manager_b, OfferingRole.MANAGER)
        instance = self.move_to("technical_assessment")
        rows = self.tasks(self.call_manager)
        self.assertEqual([row["needed"] for row in rows], [Needs.CONTRIBUTE], rows)
        self.assertIn(self.offering_b.offering.name, rows[0]["detail"])
        self.assertIn("nobody is on its team", rows[0]["detail"])
        slot = instance.contributions.get(requested_offering=self.offering_b)
        self.assertTrue(contributions.user_can_fill(self.call_manager, slot))
        # The staffed partition stays its team's.
        self.assertEqual(self.needs(self.manager_a), [Needs.CONTRIBUTE])

    def test_a_partition_with_a_team_is_not(self):
        self.move_to("technical_assessment")
        self.assertEqual(self.needs(self.call_manager), [])

    def test_a_panel_step_without_a_panel_is_completed_by_the_call_manager(self):
        self.panel.completer = ResponsibleRoles.PANEL_MEMBER
        self.panel.contributor_source = ContributorSources.NONE
        self.panel.save()
        for member in (self.panel_1, self.panel_2, self.panel_3):
            self.call.remove_user(member, CallRole.PANEL_MEMBER)
        self.move_to("panel-discussion")
        rows = self.tasks(self.call_manager)
        self.assertEqual([row["needed"] for row in rows], [Needs.DECIDE], rows)
        self.assertIn("panel member", rows[0]["detail"])
        self.assertTrue(
            permissions.user_can_act_on_active_step(self.call_manager, self.proposal)
        )

    def test_a_panel_step_with_a_panel_is_the_panels(self):
        self.panel.completer = ResponsibleRoles.PANEL_MEMBER
        self.panel.contributor_source = ContributorSources.NONE
        self.panel.save()
        self.move_to("panel-discussion")
        self.assertEqual(self.needs(self.call_manager), [])
        self.assertEqual(self.needs(self.panel_2), [Needs.DECIDE])


class ReviewersShortTest(MyTasksBase):
    """A review step short of reviews with nobody reviewing asks the call
    manager for reviewers, not to complete it."""

    def setUp(self):
        super().setUp()
        review = models.CallWorkflowStep.objects.get(
            call=self.call, key="expert_review"
        )
        review.is_enabled = True
        review.contributor_source = ContributorSources.ASSIGNED_REVIEWERS
        review.min_contributions = 2
        review.completer = ResponsibleRoles.CALL_MANAGER
        review.save()
        self.review_step = review
        proposal = self._submitted_proposal()
        self.proposal = proposal
        self.instance = self.move_to("expert_review", proposal)

    def test_the_call_manager_is_asked_for_reviewers(self):
        rows = [
            r
            for r in self.tasks(self.call_manager)
            if r["proposal_uuid"] == self.proposal.uuid.hex
        ]
        self.assertEqual([r["needed"] for r in rows], [Needs.ASSIGN], rows)
        self.assertIn("needs 2 reviews; 0 are in", rows[0]["detail"])

    def test_a_review_under_way_leaves_it_waiting(self):
        factories.StepContributionFactory(
            proposal_step=self.instance,
            contributor=self.fixture.reviewer_1,
            status=StepContributionStatuses.PENDING,
        )
        rows = [
            r
            for r in self.tasks(self.call_manager)
            if r["proposal_uuid"] == self.proposal.uuid.hex
        ]
        self.assertNotIn(Needs.ASSIGN, [r["needed"] for r in rows])


class ApplicantTest(MyTasksBase):
    def setUp(self):
        super().setUp()
        self.author = self.proposal.created_by
        self.member = structure_factories.UserFactory()
        self.proposal.add_user(self.member, ProposalRole.MEMBER)

    def test_proposal_manager_responds_to_the_reports(self):
        self.move_to("response-to-reports")
        rows = self.tasks(self.author)
        self.assertEqual([row["needed"] for row in rows], [Needs.RESPOND])
        self.assertEqual(
            rows[0]["target_section"], f"step-response-{self.response.uuid.hex}"
        )
        self.assertTrue(
            permissions.user_can_act_on_active_step(self.author, self.proposal)
        )

    def test_team_member_who_is_not_a_manager_does_not(self):
        self.move_to("response-to-reports")
        self.assertEqual(self.needs(self.member), [])
        self.assertFalse(
            permissions.user_can_act_on_active_step(self.member, self.proposal)
        )

    def test_call_manager_does_not_respond_for_the_applicant(self):
        self.move_to("response-to-reports")
        self.assertEqual(self.needs(self.call_manager), [])

    def test_proposal_manager_accepts_the_award(self):
        self.move_to("award_response")
        rows = self.tasks(self.author)
        self.assertEqual([row["needed"] for row in rows], [Needs.ACCEPT_AWARD])
        self.assertEqual(rows[0]["target_section"], "step-awarded-resources")
        self.assertEqual(self.needs(self.member), [])

    def test_accepted_proposal_leaves_the_list(self):
        self.move_to("award_response")
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        self.assertEqual(self.needs(self.author), [])


class ReviewerTest(MyTasksBase):
    def setUp(self):
        super().setUp()
        self.reviewer = structure_factories.UserFactory()
        self.call.add_user(self.reviewer, CallRole.REVIEWER)

    def test_reviewer_sees_their_review_in_progress(self):
        review = factories.ReviewFactory(proposal=self.proposal, reviewer=self.reviewer)
        rows = self.tasks(self.reviewer)
        self.assertEqual([row["needed"] for row in rows], [Needs.REVIEW])
        self.assertEqual(rows[0]["target"], my_tasks.Targets.REVIEW)
        self.assertEqual(rows[0]["target_uuid"], review.uuid.hex)
        self.assertEqual(self.needs(self.fixture.reviewer_2), [])

        review.state = models.Review.States.SUBMITTED
        review.save()
        self.assertEqual(self.needs(self.reviewer), [])

    def test_reviewer_with_a_conflict_sees_no_review(self):
        reviewer = self.reviewer
        factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=reviewer),
            proposal=self.proposal,
            status=COIStatuses.RECUSED,
        )
        self.assertEqual(self.needs(reviewer), [])

    def test_reviewer_answers_an_assignment(self):
        reviewer = structure_factories.UserFactory()
        entry = factories.CallReviewerPoolFactory(
            call=self.call, reviewer=factories.ReviewerProfileFactory(user=reviewer)
        )
        expires_at = timezone.now() + timedelta(days=3)
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=entry,
            status=AssignmentBatchStatuses.SENT,
            expires_at=expires_at,
        )
        factories.AssignmentItemFactory(batch=batch, proposal=self.proposal)
        rows = self.tasks(reviewer)
        self.assertEqual([row["needed"] for row in rows], [Needs.ANSWER_ASSIGNMENT])
        self.assertEqual(rows[0]["target"], my_tasks.Targets.ASSIGNMENTS)
        self.assertEqual(rows[0]["target_uuid"], batch.uuid.hex)
        self.assertEqual(rows[0]["proposal_name"], self.proposal.name)

        batch.expires_at = timezone.now() - timedelta(minutes=1)
        batch.save()
        self.assertEqual(self.needs(reviewer), [])

    def test_reviewer_answers_a_pool_invitation(self):
        invitee = structure_factories.UserFactory()
        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_user=invitee,
            invited_email=invitee.email,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invitation_expires_at=timezone.now() + timedelta(days=1),
        )
        rows = self.tasks(invitee)
        self.assertEqual([row["needed"] for row in rows], [Needs.ANSWER_INVITATION])
        self.assertIsNone(rows[0]["proposal_uuid"])
        self.assertEqual(rows[0]["call_uuid"], self.call.uuid.hex)


class ListTest(MyTasksBase):
    def test_anonymous_is_refused(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_paginated_with_a_count(self):
        for _ in range(2):
            self._submitted_proposal()
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(URL, {"page_size": 2})
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response["X-Result-Count"], "3")
        response = self.client.head(URL)
        self.assertEqual(response["X-Result-Count"], "3")

    def test_overdue_task_is_flagged_and_listed_first(self):
        other = self._submitted_proposal()
        other.current_step.deadline = timezone.now() - timedelta(days=1)
        other.current_step.save()
        rows = self.tasks(self.call_manager)
        self.assertEqual(rows[0]["proposal_uuid"], other.uuid.hex)
        self.assertTrue(rows[0]["is_overdue"])
        self.assertFalse(rows[1]["is_overdue"])

    def _three_deadlines(self):
        """The call manager's three tasks: one overdue, one due tomorrow and
        one due beyond the due-soon window."""
        overdue, soon = self._submitted_proposal(), self._submitted_proposal()
        for proposal, days in ((overdue, -1), (soon, 1), (self.proposal, 30)):
            proposal.current_step.deadline = timezone.now() + timedelta(days=days)
            proposal.current_step.save()
        return overdue, soon

    def test_due_narrows_the_list_to_overdue_or_due_soon_tasks(self):
        overdue, soon = self._three_deadlines()
        self.client.force_authenticate(self.call_manager)
        for due, proposal in (("overdue", overdue), ("due_soon", soon)):
            response = self.client.get(URL, {"due": due})
            self.assertEqual(
                [row["proposal_uuid"] for row in response.data],
                [proposal.uuid.hex],
            )
            self.assertEqual(self.client.head(URL, {"due": due})["X-Result-Count"], "1")

    def test_summary_counts_what_the_due_filter_lists(self):
        self._three_deadlines()
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(URL + "summary/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {"total": 3, "overdue": 1, "due_soon": 1, "due_soon_days": 7},
        )

    def test_summary_takes_the_provider_filter(self):
        self.move_to("technical_assessment")
        self.client.force_authenticate(self.manager_a)
        provider = self.offering_a.offering.customer.uuid.hex
        self.assertGreater(len(self.tasks(self.manager_a)), 0)
        self.assertEqual(
            self.client.get(URL + "summary/", {"provider_uuid": provider}).data[
                "total"
            ],
            len(self.tasks(self.manager_a)),
        )
        other = structure_factories.CustomerFactory().uuid.hex
        self.assertEqual(
            self.client.get(URL + "summary/", {"provider_uuid": other}).data["total"],
            0,
        )

    def test_staff_see_their_own_work_only(self):
        self.assertEqual(self.needs(self.fixture.staff), [])

    def test_query_count_does_not_grow_with_tasks(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                for user in (self.manager_a, self.call_manager, self.panel_1):
                    self.tasks(user)
            return len(queries)

        def add_proposals():
            for key in (
                "technical_assessment",
                "rapporteur-report",
                "panel-discussion",
            ):
                proposal = self._submitted_proposal()
                if key == "rapporteur-report":
                    self.move_to("rapporteur-assignment", proposal)
                    self.assign(self.panel_1, proposal=proposal)
                self.move_to(key, proposal)

        add_proposals()
        baseline = count()
        self.assertGreater(len(self.tasks(self.call_manager)), 1)
        add_proposals()
        add_proposals()
        self.assertEqual(count(), baseline)


class TaskViewTest(MyTasksBase):
    """A task on a proposal opens the view of the role that gave it."""

    def view(self, user):
        (row,) = self.tasks(user)
        return row["view"], row["view_customer_uuid"]

    def test_the_call_team_opens_the_call_managers_view(self):
        customer = self.call.manager.customer.uuid.hex
        self.assertEqual(
            self.view(self.call_manager), (my_tasks.Views.CALL_TEAM, customer)
        )
        # An organiser holds the call through its organisation, not a role
        # on the call.
        self.assertEqual(
            self.view(self.fixture.call_organizer_user),
            (my_tasks.Views.CALL_TEAM, customer),
        )

    def test_a_step_with_no_slot_yet_opens_on_its_actions(self):
        # Its evaluator role is held by nobody: no contribution cards to
        # open on, so the call team completes it from the step's actions.
        self.move_to("rapporteur-report")
        rows = [
            row
            for row in self.tasks(self.call_manager)
            if row["needed"] == my_tasks.Needs.COMPLETE
        ]
        self.assertTrue(rows)
        for row in rows:
            self.assertFalse(row["target_section"].startswith("step-contributions-"))

    def test_a_deciding_step_opens_on_its_actions(self):
        (row,) = self.tasks(self.call_manager)
        self.assertEqual(row["target_section"], my_tasks.STEP_ACTIONS_SECTION)

    def test_an_offering_team_opens_the_providers_view(self):
        self.move_to("technical_assessment")
        self.assertEqual(
            self.view(self.manager_a),
            (
                my_tasks.Views.OFFERING_TEAM,
                self.offering_a.offering.customer.uuid.hex,
            ),
        )

    def test_an_evaluator_opens_the_plain_page(self):
        self.move_to("rapporteur-assignment")
        self.assign(self.panel_1, is_lead=True)
        self.move_to("rapporteur-report")
        self.assertEqual(self.view(self.panel_1), (my_tasks.Views.PARTICIPANT, None))

    def test_the_applicant_opens_their_own(self):
        self.move_to("award_response")
        self.assertEqual(
            self.view(self.proposal.created_by), (my_tasks.Views.APPLICANT, None)
        )


class ReviewStepCompletionTest(ReviewersShortTest):
    def test_a_step_of_reviews_is_completed_from_its_actions(self):
        # Its reviews are shown as reviews, not as contribution cards.
        self.review_step.min_contributions = 0
        self.review_step.save()
        rows = [
            r
            for r in self.tasks(self.call_manager)
            if r["proposal_uuid"] == self.proposal.uuid.hex
        ]
        self.assertEqual([r["needed"] for r in rows], [Needs.COMPLETE], rows)
        self.assertEqual(rows[0]["target_section"], my_tasks.STEP_ACTIONS_SECTION)
