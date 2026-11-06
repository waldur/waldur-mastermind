from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class StepChecklistApiTest(test.APITestCase):
    """The step-parameterized checklist read/answer API (WAL-9484): a step's
    responsible role fills the checklist attached to that workflow step."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal_submitted
        self.proposal.state = ProposalStates.IN_REVIEW
        factories.point_at_step(self.proposal, "expert_review")
        self.proposal.save()

        self.checklist = checklist_factories.ChecklistFactory()
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist, required=True
        )
        self.call_step = factories.CallWorkflowStepFactory(
            call=self.call,
            step="expert_review",
            checklist=self.checklist,
            checklist_required=True,
            responsible_role="reviewer",
        )
        self.instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="expert_review",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.read_url = (
            factories.ProposalFactory.get_url(self.proposal, action="step-checklist")
            + "?step=expert_review"
        )
        self.write_url = (
            factories.ProposalFactory.get_url(
                self.proposal, action="submit-step-checklist-answers"
            )
            + "?step=expert_review"
        )

    def _answer_payload(self):
        return [{"question_uuid": str(self.question.uuid), "answer_data": "looks good"}]

    def test_responsible_reviewer_can_read_step_checklist(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["checklist"]["uuid"], str(self.checklist.uuid))

    def test_read_without_step_param_is_bad_request(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        url = factories.ProposalFactory.get_url(self.proposal, action="step-checklist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_responsible_reviewer_can_submit_answers_while_active(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.post(
            self.write_url, self._answer_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["completion"]["is_completed"])

    def test_applicant_cannot_answer_reviewer_step_checklist(self):
        self.client.force_authenticate(self.fixture.proposal_submitted_creator)
        response = self.client.post(
            self.write_url, self._answer_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_answers_rejected_after_step_completed(self):
        self.instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.instance.save()
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.post(
            self.write_url, self._answer_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_call_manager_answers_the_form_of_a_step_without_contributors(self):
        self.call_step.contributor_source = "none"
        self.call_step.save()
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(
            self.write_url, self._answer_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_nobody_answers_a_contributors_form_in_their_place(self):
        # A consensus report, say, is its contributors' to write: neither the
        # call team nor staff answer its form on the step itself.
        self.assertTrue(self.call_step.collects_contributions)
        for user in (self.fixture.call_manager, self.fixture.staff):
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                response = self.client.post(
                    self.write_url, self._answer_payload(), format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertIn("answered on its contributions", str(response.data))

    def test_workflow_states_exposes_checklist_status(self):
        self.client.force_authenticate(self.fixture.call_manager)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_step = {row["step"]: row for row in response.data}
        expert = by_step["expert_review"]
        self.assertIsNotNone(expert["checklist_status"])
        self.assertTrue(expert["checklist_status"]["has_checklist"])
        self.assertFalse(expert["checklist_status"]["checklist_completed"])
        self.assertEqual(expert["checklist_status"]["unanswered_required_count"], 1)

    def test_workflow_states_say_who_may_read_the_form(self):
        # As the endpoint answers each of them: the page asks only when it
        # will be given the form.
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        for user in (
            self.fixture.call_manager,
            self.fixture.reviewer_1,
            self.fixture.proposal_submitted_creator,
            self.fixture.staff,
        ):
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                states = self.client.get(url)
                if states.status_code != status.HTTP_200_OK:
                    continue
                expert = {row["step"]: row for row in states.data}["expert_review"]
                readable = (
                    self.client.get(self.read_url).status_code == status.HTTP_200_OK
                )
                self.assertEqual(expert["checklist_status"]["can_read"], readable)

    def test_workflow_states_checklist_completed_after_answer(self):
        completion = factories.contribution_completion(
            self.proposal, "expert_review", self.fixture.reviewer_1
        )
        checklist_factories.AnswerFactory(
            completion=completion,
            question=self.question,
            user=self.fixture.reviewer_1,
        )
        self.client.force_authenticate(self.fixture.call_manager)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        by_step = {row["step"]: row for row in response.data}
        self.assertTrue(
            by_step["expert_review"]["checklist_status"]["checklist_completed"]
        )


class StepChecklistCatalogueTest(test.APITestCase):
    """Call managers (non-staff) can list WORKFLOW_STEP checklists to attach to
    a step, without staff-only access to the checklist admin API (WAL-9456)."""

    def setUp(self):
        from waldur_core.checklist import enums as checklist_enums

        self.fixture = fixtures.ProposalFixture()
        self.ws_checklist = checklist_factories.ChecklistFactory(
            name="Technical eligibility",
            checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP,
        )
        self.compliance_checklist = checklist_factories.ChecklistFactory(
            name="Compliance",
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        )
        self.url = "/api/proposal-protected-calls/step_checklists/"

    def test_call_manager_lists_only_workflow_step_checklists(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [c["name"] for c in response.data]
        self.assertIn("Technical eligibility", names)
        self.assertNotIn("Compliance", names)

    def test_requires_authentication(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def names(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [c["name"] for c in response.data]

    def test_who_configures_no_call_is_refused(self):
        for user in (
            self.fixture.user,
            self.fixture.proposal_creator,
            self.fixture.reviewer_1,
            self.fixture.panel_member,
        ):
            self.client.force_authenticate(user)
            response = self.client.get(self.url)
            self.assertEqual(
                response.status_code, status.HTTP_403_FORBIDDEN, user.username
            )

    def test_staff_support_and_organisers_list_them(self):
        from waldur_core.structure.tests import factories as structure_factories

        support = structure_factories.UserFactory(is_support=True)
        for user in (
            self.fixture.staff,
            support,
            self.fixture.call_organizer_user,
            self.fixture.call_manager,
        ):
            self.assertIn("Technical eligibility", self.names(user))

    def test_another_organisations_call_copy_is_hidden(self):
        from waldur_core.checklist import enums as checklist_enums

        other = factories.CallFactory(name="Secret draft call")
        own = self.fixture.call
        for call, name in (
            (other, "Report (Secret draft call)"),
            (own, f"Report ({own.name}, 2)"),
        ):
            form = checklist_factories.ChecklistFactory(
                name=name,
                checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP,
                organization=call.manager.customer,
            )
            step = call.workflow_steps.order_by("order").first()
            step.checklist = form
            step.save()
        names = self.names(self.fixture.call_manager)
        self.assertNotIn("Report (Secret draft call)", names)
        self.assertIn(f"Report ({own.name}, 2)", names)
        self.assertIn("Report (Secret draft call)", self.names(self.fixture.staff))
