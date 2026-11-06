"""Steps for the panel chair and vice-chair, mail for the lead of an
evaluator role, and an evaluator role for the proposal's own reviewers."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, status, test

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.permissions.utils import get_users
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    contributions,
    managers,
    models,
    notification_rules,
    proposal_roles,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalRoleEligibility,
    ResponsibleRoles,
    StepKinds,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
)

MY_TASKS_URL = "http://testserver/api/proposal-my-tasks/"
RULES_URL = "/api/call-workflow-step-notification-rules/"


class PanelChairMixin(EvaluatorRoleTestMixin):
    """The rapporteur assignment is the panel chair's step."""

    def setUp(self):
        super().setUp()
        self.chair = self.panel_member()
        self.vice_chair = self.panel_member()
        self.call.panel_chair = self.chair
        self.call.panel_vice_chair = self.vice_chair
        self.call.save(update_fields=["panel_chair", "panel_vice_chair"])
        self.step.completer = ResponsibleRoles.PANEL_CHAIR
        self.step.save(update_fields=["completer"])

    def assign_rapporteurs(self, by):
        for user in (self.panel_1, self.panel_2):
            response = self.assign(user, by=by, is_lead=user == self.panel_1)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )

    def reject(self, user):
        self.proposal.refresh_from_db()
        self.client.force_authenticate(user)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="reject_workflow_step"
            ),
            {
                "step_uuid": self.proposal.current_step.uuid.hex,
                "reason": "Out of scope.",
            },
        )
        self.proposal.refresh_from_db()
        return response


class PanelChairCompleterTest(PanelChairMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_assignment_step()

    def test_the_chair_assigns_and_completes(self):
        self.assign_rapporteurs(by=self.chair)
        response = self.complete("done", user=self.chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotEqual(self.proposal.workflow_step, "rapporteur-assignment")

    def test_the_vice_chair_completes(self):
        self.assign_rapporteurs(by=self.vice_chair)
        response = self.complete("done", user=self.vice_chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_call_manager_completes(self):
        self.assign_rapporteurs(by=self.manager)
        response = self.complete("done", user=self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_another_panel_member_cannot_act(self):
        self.assign_rapporteurs(by=self.manager)
        response = self.complete("done", user=self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.reject(self.panel_3).status_code, 403)
        self.assertEqual(self.proposal.workflow_step, "rapporteur-assignment")
        response = self.assign(self.panel_3, by=self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            proposal_permissions.user_can_answer_step_checklist(
                self.panel_3, self.proposal, self.step
            )
        )

    def test_the_chair_rejects(self):
        self.assertEqual(self.reject(self.chair).status_code, status.HTTP_200_OK)

    def test_a_conflicted_chair_cannot_act(self):
        self.assign_rapporteurs(by=self.manager)
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.chair),
            proposal=self.proposal,
            call=self.call,
        )
        response = self.complete("done", user=self.chair)
        # A conflicted panel member no longer sees the proposal at all.
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertFalse(
            proposal_permissions.user_can_act_on_active_step(self.chair, self.proposal)
        )
        self.assertEqual(
            set(notification_rules._responsible_role_users(self.step, self.proposal)),
            {self.vice_chair},
        )
        # The vice-chair still may.
        response = self.complete("done", user=self.vice_chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_chair_on_the_applicant_team_cannot_act(self):
        self.assign_rapporteurs(by=self.manager)
        self.proposal.add_user(self.chair, ProposalRole.MEMBER)
        response = self.complete("done", user=self.chair)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn(
            self.chair.pk,
            proposal_permissions.panel_chair_ids(self.call, self.proposal),
        )

    def test_a_chair_who_left_the_panel_cannot_act(self):
        self.assign_rapporteurs(by=self.manager)
        # The pointer stays when the row is changed behind the handler's
        # back; the live membership check still holds.
        models.Call.objects.filter(pk=self.call.pk).update(panel_chair=self.panel_3)
        self.call.remove_user(self.panel_3, CallRole.PANEL_MEMBER)
        models.Call.objects.filter(pk=self.call.pk).update(panel_chair=self.panel_3)
        response = self.complete("done", user=self.panel_3)
        # Off the panel they no longer see the proposal at all.
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertNotIn(
            self.panel_3.pk, proposal_permissions.panel_chair_ids(self.call)
        )

    def test_chair_and_vice_chair_are_told(self):
        users = notification_rules._responsible_role_users(self.step, self.proposal)
        self.assertEqual(set(users), {self.chair, self.vice_chair})
        rule = models.CallWorkflowStepNotificationRule(
            workflow_step=self.step,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient=NotificationRuleRecipients.PANEL_CHAIR,
        )
        self.assertEqual(
            set(notification_rules.resolve_recipients(rule, self.proposal)),
            {self.chair, self.vice_chair},
        )

    def test_the_task_is_the_chairs(self):
        def needs(user):
            self.client.force_authenticate(user)
            response = self.client.get(MY_TASKS_URL, {"page_size": 100})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            return [
                row["needed"]
                for row in response.data
                if row.get("proposal_uuid") == self.proposal.uuid.hex
            ]

        self.assertTrue(needs(self.chair))
        self.assertTrue(needs(self.vice_chair))
        self.assertFalse(needs(self.panel_3))

    def test_the_hat_filter(self):
        self.client.force_authenticate(self.chair)
        response = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"as_role": ResponsibleRoles.PANEL_CHAIR},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.proposal.uuid.hex, [row["uuid"] for row in response.data])
        self.assertEqual(
            {row["call_uuid"] for row in response.data}, {self.call.uuid.hex}
        )
        self.client.force_authenticate(self.panel_3)
        response = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"as_role": ResponsibleRoles.PANEL_CHAIR},
        )
        self.assertEqual(response.data, [])

    def test_without_chairs_the_step_says_so(self):
        self.call.panel_chair = None
        self.call.panel_vice_chair = None
        self.call.save(update_fields=["panel_chair", "panel_vice_chair"])
        self.assign_rapporteurs(by=self.manager)
        instance = self.proposal.workflow_step_instances.get(call_step=self.step)
        self.assertIn("panel chair", contributions.why_step_waits(instance))


class VicePanelChairTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.url = factories.CallFactory.get_protected_url(self.call)

    def patch(self, **values):
        self.client.force_authenticate(self.manager)
        return self.client.patch(self.url, values)

    def test_set_and_unset(self):
        response = self.patch(panel_vice_chair=self.panel_2.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["panel_vice_chair_uuid"], self.panel_2.uuid.hex)
        self.assertEqual(response.data["panel_vice_chair_name"], self.panel_2.full_name)
        response = self.patch(panel_vice_chair="")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertIsNone(self.call.panel_vice_chair)

    def test_a_non_panel_member_is_refused(self):
        outsider = structure_factories.UserFactory()
        response = self.patch(panel_vice_chair=outsider.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("panel_vice_chair", response.data)

    def test_the_chair_cannot_be_vice_chair(self):
        response = self.patch(panel_chair=self.panel_2.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.patch(panel_vice_chair=self.panel_2.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("panel_vice_chair", response.data)
        self.call.refresh_from_db()
        self.call.panel_vice_chair = self.panel_2
        with self.assertRaises(serializers.ValidationError):
            self.call.clean()

    def test_a_panel_member_cannot_set_it(self):
        self.client.force_authenticate(self.panel_1)
        response = self.client.patch(
            self.url, {"panel_vice_chair": self.panel_2.uuid.hex}
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_revoking_the_panel_role_clears_it(self):
        self.call.panel_vice_chair = self.panel_2
        self.call.save(update_fields=["panel_vice_chair"])
        self.call.remove_user(self.panel_2, CallRole.PANEL_MEMBER)
        self.call.refresh_from_db()
        self.assertIsNone(self.call.panel_vice_chair)


class ChairsContributeTest(PanelChairMixin, test.APITestCase):
    """A meeting step the chair and the vice-chair each report on."""

    def setUp(self):
        super().setUp()
        self.meeting = self.add_step(
            "meeting",
            StepKinds.ASSESSMENT,
            "Committee meeting",
            2,
            outcome_set="done",
            contributor_source=f"{ContributorSources.CALL_ROLE}:panel_chair",
            completer=ResponsibleRoles.PANEL_CHAIR,
            min_contributions=2,
        )
        self.reach_assignment_step()
        self.assign_rapporteurs(by=self.manager)
        response = self.complete("done", user=self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "meeting")

    def submit_contribution(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="submit_contribution"
            ),
            {"step_uuid": self.meeting.uuid.hex},
            format="json",
        )

    def test_both_report_before_the_step_completes(self):
        response = self.submit_contribution(self.chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "meeting")
        response = self.complete("done", user=self.chair)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.submit_contribution(self.vice_chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        if self.proposal.workflow_step == "meeting":
            response = self.complete("done", user=self.vice_chair)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotEqual(self.proposal.workflow_step, "meeting")

    def test_another_panel_member_does_not_report(self):
        response = self.submit_contribution(self.panel_3)
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )
        instance = self.proposal.workflow_step_instances.get(call_step=self.meeting)
        self.assertFalse(contributions.may_contribute(self.panel_3, instance))
        self.assertTrue(contributions.may_contribute(self.vice_chair, instance))

    def test_a_missing_vice_chair_is_a_shortfall(self):
        self.assertIsNone(proposal_roles.contributors_shortfall(self.meeting))
        self.call.panel_vice_chair = None
        self.call.save(update_fields=["panel_vice_chair"])
        self.meeting.refresh_from_db()
        self.assertIn("vice-chair", proposal_roles.contributors_shortfall(self.meeting))

    def test_a_replaced_chair_no_longer_reads_through_the_slot(self):
        response = self.submit_contribution(self.chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(
            self.proposal.pk, set(managers.get_contributed_proposals(self.chair))
        )
        self.call.panel_chair = self.panel_3
        self.call.save(update_fields=["panel_chair"])
        self.assertNotIn(
            self.proposal.pk, set(managers.get_contributed_proposals(self.chair))
        )


class LeadRecipientTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_assignment_step()
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)

    def rule(self, role=None):
        return models.CallWorkflowStepNotificationRule(
            workflow_step=self.step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.PROPOSAL_ROLE_LEAD,
            proposal_role=role or self.role,
        )

    def test_only_the_lead_is_told(self):
        rule = self.rule()
        rule.full_clean()
        self.assertEqual(
            set(notification_rules.resolve_recipients(rule, self.proposal)),
            {self.panel_1},
        )
        rule.recipient = NotificationRuleRecipients.PROPOSAL_ROLE
        self.assertEqual(
            set(notification_rules.resolve_recipients(rule, self.proposal)),
            {self.panel_1, self.panel_2},
        )

    def test_a_role_without_a_lead_is_refused(self):
        role = self.any_user_role()
        with self.assertRaises(DjangoValidationError):
            self.rule(role).full_clean()
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            RULES_URL,
            {
                "workflow_step": self.step.uuid.hex,
                "trigger": NotificationRuleTriggers.STEP_COMPLETED,
                "recipient": NotificationRuleRecipients.PROPOSAL_ROLE_LEAD,
                "proposal_role": role.uuid.hex,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_role_must_be_named(self):
        rule = self.rule()
        rule.proposal_role = None
        with self.assertRaises(DjangoValidationError):
            rule.full_clean()

    def test_the_lead_and_the_holders_rules_stand_side_by_side(self):
        self.rule().save()
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.PROPOSAL_ROLE,
            proposal_role=self.role,
        )


class ProposalReviewersEligibilityTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.expert = models.CallProposalRole.objects.create(
            call=self.call,
            key="consensus-expert",
            name="Consensus expert",
            eligible_from=ProposalRoleEligibility.PROPOSAL_REVIEWERS,
            min_holders=1,
            max_holders=1,
        )
        self.step.assigns_roles.set([self.expert])
        self.reviewed = self.reviewer()
        self.unreviewed = self.reviewer()
        self.rejected = self.reviewer()

    def reviewer(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.REVIEWER)
        return user

    def review(self, user, state):
        return factories.ReviewFactory(
            proposal=self.proposal, reviewer=user, state=state
        )

    def add_reviews(self):
        self.review(self.reviewed, models.Review.States.SUBMITTED)
        self.review(self.unreviewed, models.Review.States.IN_REVIEW)
        self.review(self.rejected, models.Review.States.REJECTED)

    def test_only_a_reviewer_of_the_proposal_is_assigned(self):
        self.reach_assignment_step()
        self.add_reviews()
        response = self.assign(self.unreviewed, role=self.expert)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("user", response.data)
        response = self.assign(self.rejected, role=self.expert)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.assign(self.reviewed, role=self.expert)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_reviewer_who_left_the_pool_is_not_eligible(self):
        self.add_reviews()
        self.call.remove_user(self.reviewed, CallRole.REVIEWER)
        self.assertFalse(
            proposal_roles.is_eligible(self.reviewed, self.expert, self.proposal)
        )

    def test_candidates_are_the_proposals_reviewers(self):
        self.reach_assignment_step()
        self.add_reviews()
        teammate = self.reviewer()
        self.review(teammate, models.Review.States.SUBMITTED)
        self.proposal.add_user(teammate, ProposalRole.MEMBER)
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(
                self.proposal, action="role_assignment_candidates"
            ),
            {"role_uuid": self.expert.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [row["uuid"] for row in response.data], [self.reviewed.uuid.hex]
        )

    def test_no_call_level_shortfall(self):
        self.assertIsNone(proposal_roles.eligible_count(self.expert))
        self.assertIsNone(proposal_roles.holders_shortfall(self.expert))
        self.assertEqual(
            set(proposal_roles.eligible_users(self.expert)),
            set(get_users(self.call, role_name=RoleEnum.CALL_REVIEWER)),
        )

    def test_the_step_says_it_waits_for_reviews(self):
        self.reach_assignment_step()
        instance = self.proposal.workflow_step_instances.get(call_step=self.step)
        self.assertIn(
            "no reviewer has submitted a review",
            contributions.why_step_waits(instance),
        )
        self.add_reviews()
        self.assertNotIn(
            "no reviewer has submitted a review",
            contributions.why_step_waits(instance) or "",
        )
