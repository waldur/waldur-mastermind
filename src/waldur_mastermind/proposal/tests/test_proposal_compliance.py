"""
Tests for proposal compliance checklist integration.

This module tests the complete integration between marketplace checklists and proposals,
including checklist completion tracking, answer submission, review workflows, and all
related API endpoints.
"""

from unittest.mock import patch

from ddt import ddt
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import (
    factories as checklist_factories,
)
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures


class ProposalComplianceTestMixin:
    """Common setup for proposal compliance tests."""

    def setUp(self):
        """Set up test environment with compliance checklist."""

        # Configure proposal role permissions
        ProposalRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL)

        # Create a proposal compliance checklist first
        self.compliance_checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
            name="Proposal Compliance Checklist",
            description="Test compliance checklist for proposals",
        )

        # Create questions for the checklist
        self.required_question = checklist_factories.QuestionFactory(
            checklist=self.compliance_checklist,
            description="Is your research ethical?",
            question_type=checklist_enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
        )

        self.optional_question = checklist_factories.QuestionFactory(
            checklist=self.compliance_checklist,
            description="Additional information",
            question_type=checklist_enums.QuestionTypes.TEXT_AREA,
            required=False,
            order=2,
        )

        # Create a question that triggers review
        self.review_trigger_question = checklist_factories.QuestionFactory(
            checklist=self.compliance_checklist,
            description="Does your research involve human subjects?",
            question_type=checklist_enums.QuestionTypes.BOOLEAN,
            required=True,
            review_answer_value=True,  # 'Yes' triggers review
            operator="equals",
            order=3,
        )

        self.fixture = proposal_fixtures.ProposalFixture()

        # Assign checklist to the call BEFORE creating proposal
        self.fixture.call.compliance_checklist = self.compliance_checklist
        self.fixture.call.save()

        # Ensure checklist completion exists (for existing proposals created before checklist was assigned)
        if not self.fixture.proposal.checklist_completion:
            from django.contrib.contenttypes.models import ContentType

            proposal_content_type = ContentType.objects.get_for_model(
                self.fixture.proposal
            )
            checklist_models.ChecklistCompletion.objects.create(
                scope_content_type=proposal_content_type,
                scope_object_id=self.fixture.proposal.id,
                checklist=self.compliance_checklist,
            )

        # Ensure proposal creator has MANAGER role (since factory doesn't call perform_create)
        if not self.fixture.proposal.has_user(
            self.fixture.proposal_creator, ProposalRole.MANAGER
        ):
            self.fixture.proposal.add_user(
                self.fixture.proposal_creator,
                ProposalRole.MANAGER,
                created_by=self.fixture.proposal_creator,
            )

    def _create_checklist_answer(self, completion, question, user, answer_data):
        """Helper method to create a checklist answer using direct foreign key."""
        return checklist_models.Answer.objects.create(
            completion=completion,
            question=question,
            user=user,
            answer_data=answer_data,
        )


@ddt
class ProposalComplianceCreationTest(ProposalComplianceTestMixin, test.APITestCase):
    """Test automatic creation of checklist completion objects."""

    def test_checklist_completion_created_on_proposal_creation(self):
        """Test that ProposalChecklistCompletion is automatically created when a proposal is created."""
        # Create a completely separate call with compliance checklist for this test
        separate_call = proposal_factories.CallFactory(
            manager=self.fixture.call.manager,
            compliance_checklist=self.compliance_checklist,
        )
        separate_round = proposal_factories.RoundFactory(call=separate_call)

        # Create a new proposal in this round - signal should trigger automatically
        new_proposal = proposal_factories.ProposalFactory(
            round=separate_round, created_by=self.fixture.proposal_creator
        )

        # Verify checklist completion was created by the signal
        completion = new_proposal.checklist_completion
        self.assertIsNotNone(completion)
        self.assertEqual(completion.checklist, self.compliance_checklist)
        self.assertFalse(completion.is_completed)
        self.assertFalse(completion.requires_review)

    def test_no_checklist_completion_without_compliance_checklist(self):
        """Test that no completion object is created if call has no compliance checklist."""
        # Create call without compliance checklist
        call_without_checklist = proposal_factories.CallFactory(
            manager=self.fixture.call.manager, compliance_checklist=None
        )
        round_without_checklist = proposal_factories.RoundFactory(
            call=call_without_checklist
        )

        proposal = proposal_factories.ProposalFactory(
            round=round_without_checklist, created_by=self.fixture.proposal_creator
        )

        # Verify no checklist completion was created
        self.assertIsNone(proposal.checklist_completion)

    def test_existing_proposal_completion_status(self):
        """Test that existing proposal gets completion object and shows proper status."""
        # The fixture proposal should have completion object
        completion = self.fixture.proposal.checklist_completion
        self.assertIsNotNone(completion)

        # Initially incomplete
        self.assertFalse(completion.is_completed)
        self.assertEqual(completion.get_completion_percentage(), 0.0)

        # Should have unanswered required questions
        unanswered = completion.get_unanswered_required_questions()
        self.assertEqual(
            unanswered.count(), 2
        )  # required_question and review_trigger_question


@ddt
class ProposalComplianceAPITest(ProposalComplianceTestMixin, test.APITestCase):
    """Test compliance checklist API endpoints."""

    def test_get_compliance_checklist_as_proposal_manager(self):
        """Test proposal manager can get compliance checklist."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "checklist/"
        )
        self.client.force_authenticate(self.fixture.proposal_creator)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("checklist", data)
        self.assertIn("completion", data)
        self.assertIn("questions", data)

        # Verify checklist info
        self.assertEqual(data["checklist"]["name"], self.compliance_checklist.name)
        self.assertEqual(
            data["checklist"]["checklist_type"],
            checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        )

        # Verify completion status
        self.assertFalse(data["completion"]["is_completed"])
        self.assertEqual(data["completion"]["completion_percentage"], 0.0)

        # Verify questions
        self.assertEqual(len(data["questions"]), 3)

    def test_creator_without_manager_role_can_view_checklist(self):
        # Regression: the author reads the compliance answers they submitted
        # even without the ProposalRole.MANAGER grant (preset/imported
        # proposals, or a co-author who never held MANAGE_PROPOSAL).
        creator = structure_factories.UserFactory()
        proposal = proposal_factories.ProposalFactory(
            round=self.fixture.round, created_by=creator
        )
        url = proposal_factories.ProposalFactory.get_url(proposal) + "checklist/"
        self.client.force_authenticate(creator)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_get_compliance_checklist_without_checklist(self):
        """Test getting checklist for proposal without compliance checklist."""
        # Create proposal without compliance checklist
        call_without_checklist = proposal_factories.CallFactory(
            manager=self.fixture.call.manager, compliance_checklist=None
        )
        round_without_checklist = proposal_factories.RoundFactory(
            call=call_without_checklist
        )
        proposal_without_checklist = proposal_factories.ProposalFactory(
            round=round_without_checklist, created_by=self.fixture.proposal_creator
        )

        # Ensure proposal creator has MANAGER role
        proposal_without_checklist.add_user(
            self.fixture.proposal_creator,
            ProposalRole.MANAGER,
            created_by=self.fixture.proposal_creator,
        )

        url = (
            proposal_factories.ProposalFactory.get_url(proposal_without_checklist)
            + "checklist/"
        )
        self.client.force_authenticate(self.fixture.proposal_creator)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_compliance_answers_as_proposal_manager(self):
        """Test proposal manager can submit compliance answers."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "submit_answers/"
        )
        self.client.force_authenticate(self.fixture.proposal_creator)

        # Submit answers
        answers_data = [
            {"question_uuid": str(self.required_question.uuid), "answer_data": True},
            {
                "question_uuid": str(self.review_trigger_question.uuid),
                "answer_data": False,  # Won't trigger review
            },
        ]

        response = self.client.post(url, answers_data, format="json")
        if response.status_code != status.HTTP_200_OK:
            print(f"Error: {response.status_code}")
            print(f"Response: {response.json()}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("completion", data)
        self.assertTrue(
            data["completion"]["is_completed"]
        )  # Required questions answered
        self.assertFalse(data["completion"]["requires_review"])  # No review trigger

        # Verify answers were saved
        completion = self.fixture.proposal.checklist_completion
        self.assertEqual(completion.answers.count(), 2)

    def test_submit_answers_triggers_review(self):
        """Test that certain answers trigger review requirements."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "submit_answers/"
        )
        self.client.force_authenticate(self.fixture.proposal_creator)

        # Submit answer that triggers review
        answers_data = [
            {
                "question_uuid": str(self.review_trigger_question.uuid),
                "answer_data": True,  # This triggers review
            }
        ]

        response = self.client.post(url, answers_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertTrue(data["completion"]["requires_review"])

        # Verify review trigger summary
        triggers = data["completion"]["review_trigger_summary"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(
            triggers[0]["question"], self.review_trigger_question.description
        )

    def test_submit_invalid_answer_format(self):
        """Test submitting invalid answer format."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "submit_answers/"
        )
        self.client.force_authenticate(self.fixture.proposal_creator)

        # Submit invalid answer for boolean question
        answers_data = [
            {
                "question_uuid": str(self.required_question.uuid),
                "answer_data": "invalid_boolean",  # Should be boolean
            }
        ]

        response = self.client.post(url, answers_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_compliance_status(self):
        """Test getting compliance status."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "completion_status/"
        )
        self.client.force_authenticate(self.fixture.proposal_creator)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertFalse(data["is_completed"])
        self.assertEqual(data["completion_percentage"], 0.0)

    def test_call_manager_can_access_proposal_checklist(self):
        """Test that call managers can access proposal checklist (same permission as viewing proposal)."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "checklist/"
        )
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("checklist", data)
        self.assertIn("questions", data)

    def test_reviewer_can_access_compliance_checklist(self):
        """Test that reviewers can access compliance checklist (same permission as viewing proposal)."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "checklist/"
        )
        self.client.force_authenticate(self.fixture.reviewer_1)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("checklist", data)
        self.assertIn("questions", data)

    def test_unauthorized_answer_submission(self):
        """Test that unauthorized users cannot submit answers."""
        url = (
            proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
            + "submit_answers/"
        )
        self.client.force_authenticate(
            self.fixture.reviewer_1
        )  # Reviewer can see proposal but not manage it

        response = self.client.post(url, [], format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class CallManagerComplianceTest(ProposalComplianceTestMixin, test.APITestCase):
    """Test call manager compliance oversight features."""

    def setUp(self):
        super().setUp()
        # Create additional proposals for testing
        self.proposal2 = proposal_factories.ProposalFactory(
            round=self.fixture.round, created_by=structure_factories.UserFactory()
        )
        self.proposal3 = proposal_factories.ProposalFactory(
            round=self.fixture.round, created_by=structure_factories.UserFactory()
        )

    def test_compliance_overview(self):
        """Test call manager can get compliance overview."""
        url = (
            proposal_factories.CallFactory.get_protected_url(self.fixture.call)
            + "compliance_overview/"
        )
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("checklist", data)
        self.assertIn("proposals", data)

        # Verify checklist info
        checklist_info = data["checklist"]
        self.assertEqual(checklist_info["name"], self.compliance_checklist.name)
        self.assertEqual(checklist_info["total_questions"], 3)
        self.assertEqual(checklist_info["required_questions"], 2)

        # Verify all proposals are included (should be at least 3)
        proposals = data["proposals"]
        self.assertGreaterEqual(
            len(proposals), 3
        )  # fixture.proposal + proposal2 + proposal3

    def test_compliance_overview_without_checklist(self):
        """Test compliance overview for call without checklist."""
        # Remove checklist from call
        self.fixture.call.compliance_checklist = None
        self.fixture.call.save()

        url = (
            proposal_factories.CallFactory.get_protected_url(self.fixture.call)
            + "compliance_overview/"
        )
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIsNone(data["checklist"])
        self.assertEqual(data["proposals"], [])

    def test_review_proposal_compliance(self):
        """Test call manager can review proposal compliance."""
        # First, submit some answers that require review
        self._submit_answers_requiring_review()

        url = (
            proposal_factories.CallFactory.get_protected_url(self.fixture.call)
            + "review_proposal_compliance/"
        )
        self.client.force_authenticate(self.fixture.call_manager)

        review_data = {
            "proposal_uuid": str(self.fixture.proposal.uuid),
            "review_notes": "Compliance reviewed and approved",
        }

        response = self.client.post(url, review_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(data["reviewed_by"], self.fixture.call_manager.full_name)

        # Verify completion was updated
        completion = self.fixture.proposal.checklist_completion
        completion.refresh_from_db()
        self.assertEqual(completion.reviewed_by, self.fixture.call_manager)
        self.assertIsNotNone(completion.reviewed_at)
        self.assertEqual(completion.review_notes, "Compliance reviewed and approved")

    def test_get_proposal_compliance_answers(self):
        """Test call manager can get detailed compliance answers."""
        # Submit some answers first
        self._submit_test_answers()

        url = (
            proposal_factories.CallFactory.get_protected_url(self.fixture.call)
            + f"proposals/{self.fixture.proposal.uuid}/compliance-answers/"
        )
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("proposal", data)
        self.assertIn("completion", data)
        self.assertIn("answers", data)

        # Verify proposal info
        self.assertEqual(data["proposal"]["uuid"], str(self.fixture.proposal.uuid))

        # Verify answers
        answers = data["answers"]
        self.assertEqual(len(answers), 2)

    def test_unauthorized_call_manager_operations(self):
        """Test that non-call managers cannot access call manager endpoints."""
        url = (
            proposal_factories.CallFactory.get_protected_url(self.fixture.call)
            + "compliance_overview/"
        )
        self.client.force_authenticate(
            self.fixture.proposal_creator
        )  # Not a call manager

        response = self.client.get(url)
        # TODO: Should be 403 when endpoints are implemented
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def _submit_test_answers(self):
        """Helper method to submit test answers."""
        completion = self.fixture.proposal.checklist_completion
        self._create_checklist_answer(
            completion=completion,
            question=self.required_question,
            user=self.fixture.proposal_creator,
            answer_data=True,
        )
        self._create_checklist_answer(
            completion=completion,
            question=self.optional_question,
            user=self.fixture.proposal_creator,
            answer_data="Test additional info",
        )

    def _submit_answers_requiring_review(self):
        """Helper method to submit answers that trigger review."""
        completion = self.fixture.proposal.checklist_completion
        self._create_checklist_answer(
            completion=completion,
            question=self.review_trigger_question,
            user=self.fixture.proposal_creator,
            answer_data=True,  # Triggers review
        )
        # Update completion status to reflect the review requirement
        completion.update_completion_status()


@ddt
class ProposalSubmissionWithComplianceTest(
    ProposalComplianceTestMixin, test.APITestCase
):
    """Test proposal submission with compliance requirements."""

    def test_can_submit_regardless_of_compliance_completion(self):
        """Test that proposals can be submitted regardless of compliance completion (non-blocking)."""
        # Try to submit proposal without completing checklist
        can_submit, error = self.fixture.proposal.can_submit()
        self.assertTrue(can_submit)  # Compliance doesn't block submission
        self.assertIsNone(error)

    def test_can_submit_with_completed_compliance(self):
        """Test that proposals can be submitted with completed compliance (same as incomplete)."""
        # Complete the compliance checklist
        self._complete_compliance_checklist()

        can_submit, error = self.fixture.proposal.can_submit()
        self.assertTrue(can_submit)  # Still submittable - compliance doesn't block
        self.assertIsNone(error)

    def test_proposal_submission_validation_in_serializer(self):
        """Test that proposal serializer includes compliance status."""
        url = proposal_factories.ProposalFactory.get_url(self.fixture.proposal)
        self.client.force_authenticate(self.fixture.proposal_creator)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("compliance_status", data)
        self.assertIn("can_submit", data)

        # Compliance status shows completion but doesn't block submission
        compliance_status = data["compliance_status"]
        can_submit = data["can_submit"]

        self.assertFalse(compliance_status["is_completed"])
        self.assertTrue(
            can_submit["can_submit"]
        )  # Can always submit - compliance is for evaluation
        self.assertIsNone(can_submit["error"])

    def test_proposal_without_checklist_can_submit(self):
        """Test that proposals without compliance checklist can be submitted normally."""
        # Create proposal without compliance checklist
        call_without_checklist = proposal_factories.CallFactory(
            manager=self.fixture.call.manager, compliance_checklist=None
        )
        round_without_checklist = proposal_factories.RoundFactory(
            call=call_without_checklist
        )
        proposal_without_checklist = proposal_factories.ProposalFactory(
            round=round_without_checklist, created_by=self.fixture.proposal_creator
        )

        can_submit, error = proposal_without_checklist.can_submit()
        self.assertTrue(can_submit)
        self.assertIsNone(error)

    def _complete_compliance_checklist(self):
        """Helper method to complete compliance checklist."""
        completion = self.fixture.proposal.checklist_completion

        # Answer all required questions
        self._create_checklist_answer(
            completion=completion,
            question=self.required_question,
            user=self.fixture.proposal_creator,
            answer_data=True,
        )
        self._create_checklist_answer(
            completion=completion,
            question=self.review_trigger_question,
            user=self.fixture.proposal_creator,
            answer_data=False,  # Won't trigger review
        )

        # Update completion status
        completion.update_completion_status()


@ddt
class CallComplianceConfigurationTest(ProposalComplianceTestMixin, test.APITestCase):
    """Test call compliance checklist configuration."""

    def test_assign_compliance_checklist_to_call(self):
        """Test assigning compliance checklist to call."""
        # Create call without checklist and without any proposals
        call = proposal_factories.CallFactory(
            manager=self.fixture.call.manager, compliance_checklist=None
        )
        # Add the call manager as a manager of this call
        from waldur_core.permissions.fixtures import CallRole

        call.add_user(
            self.fixture.call_manager,
            CallRole.MANAGER,
            created_by=self.fixture.call_manager,
        )

        url = proposal_factories.CallFactory.get_protected_url(call)
        self.client.force_authenticate(self.fixture.call_manager)

        # Update call with compliance checklist
        data = {"compliance_checklist": str(self.compliance_checklist.uuid)}

        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify assignment
        call.refresh_from_db()
        self.assertEqual(call.compliance_checklist, self.compliance_checklist)

    def test_cannot_change_checklist_with_existing_proposals(self):
        """Test that checklist cannot be changed when proposals exist."""
        # Create another checklist
        new_checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE
        )

        url = proposal_factories.CallFactory.get_protected_url(self.fixture.call)
        self.client.force_authenticate(self.fixture.call_manager)

        # Try to change checklist
        data = {"compliance_checklist": str(new_checklist.uuid)}

        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Cannot change compliance checklist when proposals exist",
            str(response.content),
        )

    def test_call_serializer_includes_compliance_info(self):
        """Test that call serializer includes compliance checklist information."""
        url = proposal_factories.CallFactory.get_protected_url(self.fixture.call)
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertIn("compliance_checklist", data)
        self.assertIn("compliance_checklist_name", data)

        self.assertEqual(
            data["compliance_checklist"], str(self.compliance_checklist.uuid)
        )
        self.assertEqual(
            data["compliance_checklist_name"], self.compliance_checklist.name
        )

    def test_only_proposal_compliance_checklists_selectable(self):
        """Test that only proposal compliance checklists can be assigned to calls."""
        # Create checklist with different type
        project_checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.PROJECT_COMPLIANCE
        )

        url = proposal_factories.CallFactory.get_protected_url(self.fixture.call)
        self.client.force_authenticate(self.fixture.call_manager)

        # Try to assign project checklist to call
        data = {"compliance_checklist": str(project_checklist.uuid)}

        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ChecklistCompletionTest(ProposalComplianceTestMixin, test.APITestCase):
    """Test the ChecklistCompletion model functionality."""

    def test_completion_percentage_calculation(self):
        """Test completion percentage calculation."""
        completion = self.fixture.proposal.checklist_completion

        # Initially 0%
        self.assertEqual(completion.get_completion_percentage(), 0.0)

        # Answer one question (out of 3)
        self._create_checklist_answer(
            completion=completion,
            question=self.required_question,
            user=self.fixture.proposal_creator,
            answer_data=True,
        )

        # Should be 33.3%
        self.assertEqual(completion.get_completion_percentage(), 33.3)

        # Answer all questions
        self._create_checklist_answer(
            completion=completion,
            question=self.optional_question,
            user=self.fixture.proposal_creator,
            answer_data="Test",
        )
        self._create_checklist_answer(
            completion=completion,
            question=self.review_trigger_question,
            user=self.fixture.proposal_creator,
            answer_data=False,
        )

        # Should be 100%
        self.assertEqual(completion.get_completion_percentage(), 100.0)

    def test_update_completion_status(self):
        """Test automatic completion status updates."""
        completion = self.fixture.proposal.checklist_completion

        # Initially incomplete
        self.assertFalse(completion.is_completed)
        self.assertFalse(completion.requires_review)

        # Answer only optional question - still incomplete
        self._create_checklist_answer(
            completion=completion,
            question=self.optional_question,
            user=self.fixture.proposal_creator,
            answer_data="Test",
        )
        completion.update_completion_status()
        self.assertFalse(completion.is_completed)

        # Answer all required questions - now complete
        self._create_checklist_answer(
            completion=completion,
            question=self.required_question,
            user=self.fixture.proposal_creator,
            answer_data=True,
        )
        self._create_checklist_answer(
            completion=completion,
            question=self.review_trigger_question,
            user=self.fixture.proposal_creator,
            answer_data=True,  # Triggers review
        )
        completion.update_completion_status()

        self.assertTrue(completion.is_completed)
        self.assertTrue(completion.requires_review)

    def test_review_trigger_summary(self):
        """Test review trigger summary functionality."""
        completion = self.fixture.proposal.checklist_completion

        # Answer question that triggers review
        self._create_checklist_answer(
            completion=completion,
            question=self.review_trigger_question,
            user=self.fixture.proposal_creator,
            answer_data=True,
        )
        completion.update_completion_status()

        summary = completion.get_review_trigger_summary()
        self.assertEqual(len(summary), 1)

        trigger = summary[0]
        self.assertEqual(trigger["question"], self.review_trigger_question.description)
        self.assertEqual(trigger["answer"], True)
        self.assertEqual(trigger["trigger_value"], True)
        self.assertEqual(trigger["operator"], "equals")

    def test_unanswered_required_questions(self):
        """Test getting unanswered required questions."""
        completion = self.fixture.proposal.checklist_completion

        # Initially all required questions unanswered
        unanswered = completion.get_unanswered_required_questions()
        self.assertEqual(
            unanswered.count(), 2
        )  # required_question and review_trigger_question

        # Answer one required question
        self._create_checklist_answer(
            completion=completion,
            question=self.required_question,
            user=self.fixture.proposal_creator,
            answer_data=True,
        )

        unanswered = completion.get_unanswered_required_questions()
        self.assertEqual(unanswered.count(), 1)  # Only review_trigger_question left

        # Answer remaining required question
        self._create_checklist_answer(
            completion=completion,
            question=self.review_trigger_question,
            user=self.fixture.proposal_creator,
            answer_data=False,
        )

        unanswered = completion.get_unanswered_required_questions()
        self.assertEqual(unanswered.count(), 0)


class ProposalComplianceSignalsTest(ProposalComplianceTestMixin, test.APITestCase):
    """Test Django signals related to proposal compliance."""

    def test_checklist_completion_created_via_signal(self):
        """Test that checklist completion is created via post_save signal."""
        # Create a new call with compliance checklist for this test
        test_call = proposal_factories.CallFactory(
            manager=self.fixture.call.manager,
            compliance_checklist=self.compliance_checklist,
        )
        test_round = proposal_factories.RoundFactory(call=test_call)

        # Create proposal without checklist first (by temporarily removing checklist)
        test_call.compliance_checklist = None
        test_call.save()

        proposal = proposal_factories.ProposalFactory(
            round=test_round, created_by=self.fixture.proposal_creator
        )

        # Verify no completion exists yet
        self.assertIsNone(proposal.checklist_completion)

        # Now add checklist back and trigger signal manually
        test_call.compliance_checklist = self.compliance_checklist
        test_call.save()

        # Simulate the signal call that would happen on proposal creation
        from waldur_mastermind.proposal import handlers

        handlers.create_checklist_completion(
            sender=proposal_models.Proposal, instance=proposal, created=True
        )

        # Verify completion was created
        completion = proposal.checklist_completion
        self.assertIsNotNone(completion)
        self.assertEqual(completion.checklist, self.compliance_checklist)
        self.assertFalse(completion.is_completed)

    def test_answer_save_triggers_completion_update(self):
        """Test that saving an answer triggers completion status update."""
        completion = self.fixture.proposal.checklist_completion

        # Mock the update method to verify it's called
        with patch.object(completion, "update_completion_status") as mock_update:
            # Create answer
            self._create_checklist_answer(
                completion=completion,
                question=self.required_question,
                user=self.fixture.proposal_creator,
                answer_data=True,
            )

            # Update should have been called
            mock_update.assert_called_once()

    def test_checklist_completion_deleted_with_proposal(self):
        """Test that ChecklistCompletion is deleted when Proposal is deleted."""
        # Create a separate proposal for this test
        test_call = proposal_factories.CallFactory(
            manager=self.fixture.call.manager,
            compliance_checklist=self.compliance_checklist,
        )
        test_round = proposal_factories.RoundFactory(call=test_call)
        test_proposal = proposal_factories.ProposalFactory(
            round=test_round, created_by=self.fixture.proposal_creator
        )

        # Verify completion was created
        completion = test_proposal.checklist_completion
        self.assertIsNotNone(completion)
        completion_id = completion.id

        # Delete the proposal
        test_proposal.delete()

        # Verify completion was also deleted
        completion_exists = checklist_models.ChecklistCompletion.objects.filter(
            id=completion_id
        ).exists()
        self.assertFalse(
            completion_exists,
            "ChecklistCompletion should be deleted when Proposal is deleted",
        )
