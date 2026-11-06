"""A call manager who applies to their own call is the applicant on that proposal.

Managing the call grants oversight of its proposals -- reading and acting on
their evaluation, editing them outside what the applicant may -- but never of
a proposal whose applicant team the manager is on. There they hold what any
other applicant holds, and their other proposals' oversight is unchanged.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.proposal import (
    contributions,
    models,
    my_tasks,
    step_inputs,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    ContributionOutcomes,
    ProposalStates,
    TransitionModes,
)
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_applicant_team import TeamTestMixin
from waldur_mastermind.proposal.tests.test_step_contributions import (
    PerOfferingAssessmentBase,
)


class CallManagerOnTeamEvaluationTest(PerOfferingAssessmentBase):
    """The call manager on the team, at a provider assessment of their
    proposal that the applicant does not see."""

    def setUp(self):
        super().setUp()
        self.slot = self._slot(self.requested_a)
        response = self._answer(self.manager_a, self.slot, text="Provider note")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.add_user(self.call_manager, ProposalRole.MANAGER)

    def test_they_do_not_read_the_assessment_answers(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            self._url("step-checklist-responses")
            + f"?step_uuid={self.call_step.uuid.hex}"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(contributions.user_can_read(self.call_manager, self.slot))
        self.assertFalse(contributions.oversees_step(self.call_manager, self.proposal))

    def test_they_do_not_answer_or_submit_a_provider_slot(self):
        self.assertFalse(contributions.user_can_fill(self.call_manager, self.slot))
        response = self._answer(self.call_manager, self.slot, text="Rewritten")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self._submit(
            self.call_manager, self.slot, ContributionOutcomes.ACCEPTED
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.slot.refresh_from_db()
        self.assertIsNone(self.slot.submitted_at)

    def test_they_do_not_read_the_per_offering_outcomes(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._url("offering_assessments"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_they_do_not_read_step_inputs_as_the_call_team(self):
        self.assertFalse(step_inputs.oversees_inputs(self.call_manager, self.proposal))

    def test_the_checklist_is_not_theirs_to_answer_or_read(self):
        self.assertFalse(
            proposal_permissions.user_can_answer_step_checklist(
                self.call_manager, self.proposal, self.call_step
            )
        )
        self.assertFalse(
            proposal_permissions.user_can_read_step_checklist(
                self.call_manager, self.proposal, self.call_step
            )
        )

    def test_no_stand_in_slots_on_their_own_proposal(self):
        standing = my_tasks.Standing(self.call_manager)
        work = my_tasks.StepWork(standing, self.instance, [self.slot])
        self.assertEqual(work.stand_in_slots(), [])

    def test_they_still_oversee_the_other_proposals_of_the_call(self):
        other = factories.ProposalFactory(round=self.fixture.round)
        self.assertTrue(
            proposal_permissions.oversees_proposal_call(self.call_manager, other)
        )
        self.assertTrue(
            proposal_permissions.manages_proposal_workflow(self.call_manager, other)
        )


class CallManagerOnTeamProposalTest(TeamTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call_manager = self.fixture.call_manager
        self.proposal.add_user(self.call_manager, ProposalRole.MANAGER)

    def attach(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="attach_document"),
            {"file": SimpleUploadedFile("a.pdf", b"%PDF-1.4\n%%EOF\n")},
            format="multipart",
        )

    def test_they_do_not_attach_documents_in_review(self):
        self.submit()
        self.assertEqual(self.attach(self.author).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            self.attach(self.call_manager).status_code, status.HTTP_409_CONFLICT
        )

    def test_they_do_not_advance_their_own_workflow(self):
        models.CallWorkflowStep.objects.filter(call=self.call, key="statement").update(
            transition_mode=TransitionModes.MANUAL
        )
        self.reach_statement()
        response = self.complete("submitted", user=self.author)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(self.proposal, "advance_workflow_step")
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "statement")
        standing = my_tasks.Standing(self.call_manager)
        self.assertEqual(my_tasks._advance_tasks(standing), [])


class CallManagerOnTeamReviewsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call_manager = self.fixture.call_manager
        self.own = self.fixture.proposal
        # Under evaluation: the applicant reads no review of it yet.
        self.own.state = ProposalStates.IN_REVIEW
        self.own.save(update_fields=["state"])
        self.own.add_user(self.call_manager, ProposalRole.MANAGER)
        self.own_review = factories.ReviewFactory(
            proposal=self.own,
            reviewer=self.fixture.reviewer_1,
            summary_private_comment="Only for the call team",
        )
        self.other_review = factories.ReviewFactory(
            proposal=factories.ProposalFactory(round=self.fixture.round),
            reviewer=self.fixture.reviewer_1,
        )

    def test_they_do_not_read_the_reviews_of_their_own_proposal(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(factories.ReviewFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        listed = [row["uuid"] for row in response.data]
        self.assertIn(self.other_review.uuid.hex, listed)
        self.assertNotIn(self.own_review.uuid.hex, listed)
        response = self.client.get(factories.ReviewFactory.get_url(self.own_review))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
