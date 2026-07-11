from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


class TechnicalAssessmentResponsesTest(test.APITestCase):
    """The threaded technical-assessment read (WAL-9337): every technical
    reviewer's decision + comment, grouped by reviewer."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal_submitted
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "technical_assessment"
        self.proposal.save()

        self.checklist = checklist_factories.ChecklistFactory(
            name="Technical assessment",
            checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP,
        )
        self.decision = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Decision",
            question_type=checklist_enums.QuestionTypes.SINGLE_SELECT,
            order=0,
        )
        self.accepted_option = checklist_factories.QuestionOptionFactory(
            question=self.decision, label="Accepted", order=0
        )
        self.rejected_option = checklist_factories.QuestionOptionFactory(
            question=self.decision, label="Rejected", order=1
        )
        self.comment = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Comment",
            question_type=checklist_enums.QuestionTypes.TEXT_AREA,
            order=1,
        )
        self.call_step = factories.CallWorkflowStepFactory(
            call=self.call,
            step="technical_assessment",
            checklist=self.checklist,
            responsible_role="offering_manager",
            applicant_visible=False,
        )
        self.completion = self.proposal.ensure_checklist_completion_for(self.checklist)

        self.reviewer_a = structure_factories.UserFactory()
        self.reviewer_b = structure_factories.UserFactory()
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="step-checklist-responses"
        )

    def _answer(self, user, decision_option, comment_text):
        checklist_factories.AnswerFactory(
            completion=self.completion,
            question=self.decision,
            user=user,
            answer_data=[str(decision_option.uuid)],
        )
        checklist_factories.AnswerFactory(
            completion=self.completion,
            question=self.comment,
            user=user,
            answer_data=comment_text,
        )

    def _get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url, {"step": "technical_assessment"})

    def test_offering_manager_can_retrieve_requested_proposal(self):
        # An offering manager may retrieve a NON-DRAFT proposal that requested
        # one of their accepted offerings (so they can answer/view the
        # technical_assessment). filter_proposals must include it.
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        manager = self.fixture.offering_fixture.offering_manager
        self.client.force_authenticate(manager)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_offering_manager_cannot_retrieve_draft_proposal(self):
        # Drafts are the applicant's WIP — not exposed to technical reviewers.
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        manager = self.fixture.offering_fixture.offering_manager
        self.client.force_authenticate(manager)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_offering_manager_cannot_retrieve_unrequested_proposal(self):
        # A proposal on the same call that did NOT request this manager's
        # offering must stay invisible to them (no cross-provider exposure).
        manager = self.fixture.offering_fixture.offering_manager
        self.fixture.requested_offering_accepted  # manager holds a call offering
        self.client.force_authenticate(manager)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_call_manager_sees_each_reviewer_grouped(self):
        self._answer(self.reviewer_a, self.accepted_option, "Feasible")
        self._answer(self.reviewer_b, self.rejected_option, "Too costly")
        response = self._get(self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        by_user = {str(g["user_uuid"]): g for g in response.data}
        group_a = by_user[str(self.reviewer_a.uuid)]
        self.assertEqual(group_a["user_full_name"], self.reviewer_a.full_name)
        self.assertEqual(len(group_a["answers"]), 2)

    def test_single_select_answer_display_resolves_option_label(self):
        self._answer(self.reviewer_a, self.accepted_option, "Feasible")
        response = self._get(self.fixture.call_manager)
        answers = response.data[0]["answers"]
        decision = next(a for a in answers if a["question_description"] == "Decision")
        self.assertEqual(decision["answer_display"], "Accepted")
        comment = next(a for a in answers if a["question_description"] == "Comment")
        self.assertEqual(comment["answer_display"], "Feasible")

    def test_offering_manager_permission_allows_view(self):
        # The view permission itself allows offering managers of the call (they
        # may see peers' technical assessments). Proposal-level visibility for
        # offering managers is enforced by filter_proposals (restricted to
        # non-draft proposals that requested their offering) -- covered by the
        # request-path tests below; this asserts the permission layer directly.
        from waldur_mastermind.proposal import permissions

        self.fixture.requested_offering_accepted
        manager = self.fixture.offering_fixture.offering_manager
        self.assertTrue(
            permissions._user_holds_offering_manager_for_call(manager, self.call)
        )

    def test_applicant_denied_when_not_visible(self):
        self._answer(self.reviewer_a, self.accepted_option, "Feasible")
        response = self._get(self.proposal.created_by)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_applicant_allowed_when_visible(self):
        self.call_step.applicant_visible = True
        self.call_step.save()
        self._answer(self.reviewer_a, self.accepted_option, "Feasible")
        response = self._get(self.proposal.created_by)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_empty_when_no_answers(self):
        response = self._get(self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    # --- Security review fixes -------------------------------------------------

    def _link_offering_to_proposal(self):
        # Make the offering manager a legitimate technical reviewer for this
        # proposal by having it request their accepted offering (so it is visible
        # to them under the tightened filter_proposals).
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
        )

    def _step_checklist_url(self):
        return factories.ProposalFactory.get_url(self.proposal, action="step-checklist")

    def test_applicant_cannot_read_evaluation_step_checklist(self):
        # HIGH-1: the applicant must NOT read an evaluation step's answers via the
        # step_checklist GET (it would bypass applicant_visible).
        self.client.force_authenticate(self.proposal.created_by)
        response = self.client.get(
            self._step_checklist_url(), {"step": "technical_assessment"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_offering_manager_can_read_evaluation_step_checklist(self):
        self._link_offering_to_proposal()
        self.client.force_authenticate(self.fixture.offering_fixture.offering_manager)
        response = self.client.get(
            self._step_checklist_url(), {"step": "technical_assessment"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_step_checklist_get_returns_only_requesting_users_answer(self):
        # HIGH-1b: a peer's answer must not seed another reviewer's editing form.
        self._link_offering_to_proposal()
        self._answer(self.reviewer_a, self.accepted_option, "Peer private note")
        self.client.force_authenticate(self.fixture.offering_fixture.offering_manager)
        response = self.client.get(
            self._step_checklist_url(),
            {"step": "technical_assessment", "include_all": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for question in response.data["questions"]:
            self.assertIsNone(question["existing_answer"])

    def test_blind_review_hides_peers_from_offering_manager(self):
        # MED-HIGH-2: under blind_review a peer offering manager can't see the
        # thread; the call manager (oversight) still can.
        self.call_step.blind_review = True
        self.call_step.save()
        self._link_offering_to_proposal()
        self._answer(self.reviewer_a, self.accepted_option, "Feasible")
        self.assertEqual(
            self._get(self.fixture.offering_fixture.offering_manager).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self._get(self.fixture.call_manager).status_code, status.HTTP_200_OK
        )

    def test_applicant_reviewer_identity_anonymized_when_call_hides_it(self):
        # Review #5: applicant sees decision+comment but not who said it when the
        # call hides reviewer identities from submitters.
        self.call_step.applicant_visible = True
        self.call_step.save()
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.save()
        self._answer(self.reviewer_a, self.accepted_option, "Feasible")
        response = self._get(self.proposal.created_by)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data[0]["user_uuid"])
        self.assertNotEqual(
            response.data[0]["user_full_name"], self.reviewer_a.full_name
        )
        self.assertTrue(response.data[0]["answers"])  # decision still shown

    def test_applicant_sees_reviewer_identity_when_call_shows_it(self):
        self.call_step.applicant_visible = True
        self.call_step.save()
        self.call.reviewer_identity_visible_to_submitters = True
        self.call.save()
        self._answer(self.reviewer_a, self.accepted_option, "Feasible")
        response = self._get(self.proposal.created_by)
        self.assertEqual(response.data[0]["user_full_name"], self.reviewer_a.full_name)
