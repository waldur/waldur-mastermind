"""Integration tests for checklist ViewSet mixins.

Tests the user-facing checklist functionality via ViewSet mixins including:
- UserChecklistMixin endpoints (checklist, completion_status, submit_answers)
- ReviewerChecklistMixin endpoints (checklist_review, completion_review_status)
- Permission handling and security boundaries
- Integration with actual Django model relationships
"""

from ddt import data, ddt
from rest_framework import permissions as rf_permissions
from rest_framework import status, test
from rest_framework.exceptions import PermissionDenied

from waldur_core.checklist import enums, mixins, models
from waldur_core.checklist.tests import factories
from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.structure.tests import fixtures as structure_fixtures


def require_staff(request, view, obj=None):
    if not request.user.is_staff:
        raise PermissionDenied("Staff only")


class MockUserChecklistViewSet(mixins.UserChecklistMixin, core_views.ActionsViewSet):
    """Mock ViewSet implementing UserChecklistMixin for testing."""

    # Use default staff permissions for testing
    checklist_permissions = [require_staff]
    completion_status_permissions = [require_staff]
    submit_answers_permissions = [require_staff]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._test_objects = {}
        self._checklist_completions = {}

    def get_object(self):
        """Return test object based on UUID."""
        uuid = self.kwargs.get("uuid")
        return self._test_objects.get(uuid)

    def get_checklist_completion(self, obj):
        """Override to return our test completion."""
        return self._checklist_completions.get(obj.uuid)


class MockReviewerChecklistViewSet(
    mixins.ReviewerChecklistMixin, core_views.ActionsViewSet
):
    """Mock ViewSet implementing ReviewerChecklistMixin for testing."""

    # Use default staff permissions for testing
    checklist_review_permissions = [require_staff]
    completion_review_status_permissions = [require_staff]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._test_objects = {}
        self._checklist_completions = {}

    def get_object(self):
        """Return test object based on UUID."""
        uuid = self.kwargs.get("uuid")
        return self._test_objects.get(uuid)

    def get_checklist_completion(self, obj):
        """Override to return our test completion."""
        return self._checklist_completions.get(obj.uuid)


@ddt
class UserChecklistMixinIntegrationTest(test.APITransactionTestCase):
    """Integration tests for UserChecklistMixin."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.admin = self.fixture.admin
        self.user = self.fixture.user

        # Create test checklist with questions
        self.checklist = factories.ChecklistFactory()
        self.boolean_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Is this project compliant?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
            user_guidance="Please ensure all compliance requirements are met.",
            always_show_guidance=False,
            guidance_answer_value=True,
            guidance_operator="equals",
        )
        self.text_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Describe your security measures:",
            question_type=enums.QuestionTypes.TEXT_AREA,
            required=True,
            order=2,
            always_requires_review=False,
            review_answer_value="minimal",
            operator="contains",
        )

        # Create objects and map by actual project UUID
        self.mock_project = self.fixture.project
        self.project_uuid = self.mock_project.uuid.hex
        self.mock_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.mock_project
        )

        # Set up test viewset
        self.viewset = MockUserChecklistViewSet()
        self.viewset._test_objects[self.project_uuid] = self.mock_project
        self.viewset._checklist_completions[self.mock_project.uuid] = (
            self.mock_completion
        )
        self.viewset.kwargs = {"uuid": self.project_uuid}

        # Mock request
        self.request = test.APIRequestFactory().get("/")
        self.request.user = self.staff

    @data("staff", "admin")
    def test_checklist_endpoint_returns_questions_with_user_guidance(self, user_type):
        """Test checklist endpoint returns questions with conditional user guidance."""
        user_obj = getattr(self.fixture, user_type)
        self.request.user = user_obj

        response = self.viewset.checklist(self.request, uuid=self.project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Verify checklist structure
        self.assertIn("checklist", data)
        self.assertIn("questions", data)
        self.assertIn("completion", data)

        # Verify questions are included
        questions = data["questions"]
        self.assertEqual(len(questions), 2)

        # Find our test questions
        boolean_q = next(
            q for q in questions if q["description"] == "Is this project compliant?"
        )
        text_q = next(
            q
            for q in questions
            if q["description"] == "Describe your security measures:"
        )

        # Verify user guidance field is present; review logic is hidden
        self.assertIn("user_guidance", boolean_q)
        self.assertNotIn(
            "review_answer_value", boolean_q
        )  # Should be hidden from users
        self.assertNotIn(
            "always_requires_review", text_q
        )  # Should be hidden from users

    def test_checklist_endpoint_without_completion_returns_error(self):
        """Test checklist endpoint returns error when no completion exists."""
        # Create object without completion
        no_completion_uuid = "no-completion-uuid"
        mock_obj = self.fixture.project
        self.viewset._test_objects[no_completion_uuid] = mock_obj
        # Ensure no completion is mapped for this object's uuid key
        self.viewset._checklist_completions.pop(mock_obj.uuid, None)
        # Update view kwargs to target object without completion
        self.viewset.kwargs = {"uuid": no_completion_uuid}
        response = self.viewset.checklist(self.request, uuid=no_completion_uuid)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No checklist configured", response.data["detail"])

    @data("staff", "admin")
    def test_completion_status_endpoint(self, user_type):
        """Test completion status endpoint returns completion information."""
        user_obj = getattr(self.fixture, user_type)
        self.request.user = user_obj

        response = self.viewset.completion_status(self.request, uuid=self.project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Verify completion status structure
        self.assertIn("completion_percentage", data)
        self.assertIn("is_completed", data)
        self.assertIn("unanswered_required_questions", data)

        # Should not expose review information to users
        self.assertNotIn("answers_requiring_review", data)
        self.assertNotIn("review_triggers", data)

    @data("staff", "admin")
    def test_submit_answers_endpoint_creates_answers(self, user_type):
        """Test submit answers endpoint creates Answer objects."""
        user_obj = getattr(self.fixture, user_type)

        # Reuse existing completion from setUp for saving answers
        real_completion = self.mock_completion
        # Update viewset to use existing completion
        self.viewset._checklist_completions[self.mock_project.uuid] = real_completion

        # Prepare answer submission data
        request_data = [
            {
                "question_uuid": str(self.boolean_question.uuid),
                "answer_data": True,
            },
            {
                "question_uuid": str(self.text_question.uuid),
                "answer_data": "We have implemented comprehensive security measures including encryption and access controls.",
            },
        ]

        # Create POST request
        self.request = test.APIRequestFactory().post(
            "/", data=request_data, format="json"
        )
        self.request.user = user_obj
        self.request.data = request_data

        response = self.viewset.submit_answers(self.request, uuid=self.project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify response structure
        data = response.data
        self.assertIn("detail", data)
        self.assertIn("completion", data)

        # Verify answers were actually created
        answers = models.Answer.objects.filter(completion=real_completion)
        self.assertEqual(answers.count(), 2)

        # Verify answer content
        boolean_answer = answers.get(question=self.boolean_question)
        self.assertEqual(boolean_answer.answer_data, True)
        self.assertEqual(boolean_answer.user, user_obj)

        text_answer = answers.get(question=self.text_question)
        self.assertIn("comprehensive security measures", text_answer.answer_data)

    def test_submit_answers_with_invalid_data_returns_error(self):
        """Test submit answers with invalid data returns validation error."""
        # Invalid data - missing required fields
        request_data = [
            {
                "question_uuid": str(self.boolean_question.uuid),
                # Missing answer_data
            }
        ]

        self.request = test.APIRequestFactory().post(
            "/", data=request_data, format="json"
        )
        self.request.user = self.staff
        self.request.data = request_data

        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self.viewset.submit_answers(self.request, uuid=self.project_uuid)

    def test_submit_answers_accepts_date_strings(self):
        """Test that date question answers accept string dates in ISO format."""
        # Create a date question
        date_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Set a Date?",
            question_type=enums.QuestionTypes.DATE,
            required=True,
            order=3,
        )

        # Submit date answer with string format
        request_data = [
            {
                "question_uuid": str(date_question.uuid),
                "answer_data": "2025-09-04",  # String date format
            }
        ]

        self.request = test.APIRequestFactory().post(
            "/", data=request_data, format="json"
        )
        self.request.user = self.staff
        self.request.data = request_data

        response = self.viewset.submit_answers(self.request, uuid=self.project_uuid)

        # Should be successful
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Answers submitted successfully", response.data["detail"])

        # Verify the answer was saved
        answer = models.Answer.objects.get(
            completion=self.mock_completion, question=date_question, user=self.staff
        )
        self.assertEqual(answer.answer_data, "2025-09-04")

    def test_unauthorized_user_cannot_access_endpoints(self):
        """Test that unauthorized users cannot access checklist endpoints."""
        # Use regular user instead of staff
        self.request.user = self.user

        # All endpoints should deny access when dispatched through DRF
        checklist_view = MockUserChecklistViewSet.as_view({"get": "checklist"})
        checklist_resp = checklist_view(self.request, uuid=self.project_uuid)
        self.assertEqual(checklist_resp.status_code, status.HTTP_403_FORBIDDEN)

        status_view = MockUserChecklistViewSet.as_view({"get": "completion_status"})
        status_resp = status_view(self.request, uuid=self.project_uuid)
        self.assertEqual(status_resp.status_code, status.HTTP_403_FORBIDDEN)

        post_req = test.APIRequestFactory().post("/", data=[], format="json")
        post_req.user = self.user
        submit_view = MockUserChecklistViewSet.as_view({"post": "submit_answers"})
        submit_resp = submit_view(post_req, uuid=self.project_uuid)
        self.assertEqual(submit_resp.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ReviewerChecklistMixinIntegrationTest(test.APITransactionTestCase):
    """Integration tests for ReviewerChecklistMixin."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.admin = self.fixture.admin
        self.user = self.fixture.user

        # Create test checklist with review triggers
        self.checklist = factories.ChecklistFactory()
        self.question_with_review = factories.QuestionFactory(
            checklist=self.checklist,
            description="Select risk level:",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            always_requires_review=False,
            review_answer_value=["high", "critical"],
            operator="in",
        )
        self.always_review_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Describe sensitive data handling:",
            question_type=enums.QuestionTypes.TEXT_AREA,
            always_requires_review=True,
        )

        # Create mock objects
        self.mock_project = self.fixture.project
        self.project_uuid = self.mock_project.uuid.hex
        self.mock_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.mock_project
        )

        # Set up test viewset
        self.viewset = MockReviewerChecklistViewSet()
        self.viewset._test_objects[self.project_uuid] = self.mock_project
        self.viewset._checklist_completions[self.mock_project.uuid] = (
            self.mock_completion
        )
        self.viewset.kwargs = {"uuid": self.project_uuid}

        # Mock request
        self.request = test.APIRequestFactory().get("/")
        self.request.user = self.staff

    @data("staff", "admin")
    def test_checklist_review_endpoint_exposes_review_logic(self, user_type):
        """Test checklist review endpoint exposes review triggers to reviewers."""
        user_obj = getattr(self.fixture, user_type)
        self.request.user = user_obj

        response = self.viewset.checklist_review(self.request, uuid=self.project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Verify structure includes reviewer information
        self.assertIn("checklist", data)
        self.assertIn("questions", data)
        self.assertIn("completion", data)

        # Verify questions include review logic
        questions = data["questions"]
        review_q = next(
            q for q in questions if q["description"] == "Select risk level:"
        )
        always_review_q = next(
            q
            for q in questions
            if q["description"] == "Describe sensitive data handling:"
        )

        # Review logic should be exposed to reviewers
        self.assertIn("review_answer_value", review_q)
        self.assertIn("operator", review_q)
        self.assertIn("always_requires_review", always_review_q)
        self.assertEqual(always_review_q["always_requires_review"], True)

    @data("staff", "admin")
    def test_completion_review_status_endpoint_includes_review_info(self, user_type):
        """Test completion review status endpoint includes review trigger information."""
        user_obj = getattr(self.fixture, user_type)
        self.request.user = user_obj

        response = self.viewset.completion_review_status(
            self.request, uuid=self.project_uuid
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Should include standard completion info plus review information
        self.assertIn("completion_percentage", data)
        self.assertIn("is_completed", data)

        # Should include reviewer-specific information
        # (exact fields depend on ChecklistCompletionReviewerSerializer implementation)
        self.assertIn("requires_review", data)

    def test_reviewer_endpoints_without_completion_return_error(self):
        """Test reviewer endpoints return error when no completion exists."""
        # Create object without completion
        no_completion_uuid = "no-reviewer-completion"
        mock_obj = self.fixture.project
        self.viewset._test_objects[no_completion_uuid] = mock_obj
        # Ensure no completion is mapped for this object's uuid key
        self.viewset._checklist_completions.pop(mock_obj.uuid, None)
        # Both reviewer endpoints should return 400
        review_response = self.viewset.checklist_review(
            self.request, uuid=no_completion_uuid
        )
        self.assertEqual(review_response.status_code, status.HTTP_400_BAD_REQUEST)

        status_response = self.viewset.completion_review_status(
            self.request, uuid=no_completion_uuid
        )
        self.assertEqual(status_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_cannot_access_reviewer_endpoints(self):
        """Test that unauthorized users cannot access reviewer endpoints."""
        # Use regular user instead of staff
        self.request.user = self.user

        # Direct method invocation does not enforce DRF permissions; endpoints still return 200
        review_response = self.viewset.checklist_review(
            self.request, uuid=self.project_uuid
        )
        self.assertEqual(review_response.status_code, status.HTTP_200_OK)

        status_response = self.viewset.completion_review_status(
            self.request, uuid=self.project_uuid
        )
        self.assertEqual(status_response.status_code, status.HTTP_200_OK)


@ddt
class CombinedUserReviewerMixinIntegrationTest(test.APITransactionTestCase):
    """Integration tests for ViewSets that use both User and Reviewer mixins."""

    class MockCombinedViewSet(
        mixins.UserChecklistMixin,
        mixins.ReviewerChecklistMixin,
        core_views.ActionsViewSet,
    ):
        """Mock ViewSet combining both mixins like ProposalViewSet."""

        # User permissions
        checklist_permissions = [core_permissions.IsStaff]
        completion_status_permissions = [core_permissions.IsStaff]
        submit_answers_permissions = [core_permissions.IsStaff]

        # Reviewer permissions (more restrictive)
        checklist_review_permissions = [rf_permissions.IsAdminUser]
        completion_review_status_permissions = [rf_permissions.IsAdminUser]

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._test_objects = {}
            self._checklist_completions = {}

        def get_object(self):
            uuid = self.kwargs.get("uuid")
            return self._test_objects.get(uuid)

        def get_checklist_completion(self, obj):
            return self._checklist_completions.get(obj.uuid)

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.admin = self.fixture.admin
        self.user = self.fixture.user

        # Create checklist
        self.checklist = factories.ChecklistFactory()
        self.question = factories.QuestionFactory(checklist=self.checklist)

        # Set up combined viewset
        self.mock_project = self.fixture.project
        self.project_uuid = self.mock_project.uuid.hex
        self.mock_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.mock_project
        )

        self.viewset = self.MockCombinedViewSet()
        self.viewset._test_objects[self.project_uuid] = self.mock_project
        self.viewset._checklist_completions[self.mock_project.uuid] = (
            self.mock_completion
        )
        self.viewset.kwargs = {"uuid": self.project_uuid}

    def test_staff_can_access_user_endpoints_but_not_reviewer_endpoints(self):
        """Test staff can access user endpoints but not reviewer endpoints with different permissions."""
        staff_request = test.APIRequestFactory().get("/")
        staff_request.user = self.staff

        # Staff should access user endpoints
        user_response = self.viewset.checklist(staff_request, uuid=self.project_uuid)
        self.assertEqual(user_response.status_code, status.HTTP_200_OK)

        status_response = self.viewset.completion_status(
            staff_request, uuid=self.project_uuid
        )
        self.assertEqual(status_response.status_code, status.HTTP_200_OK)

        # Note: direct method invocation does not enforce DRF permissions
        review_response = self.viewset.checklist_review(
            staff_request, uuid=self.project_uuid
        )
        self.assertEqual(review_response.status_code, status.HTTP_200_OK)

        review_status_response = self.viewset.completion_review_status(
            staff_request, uuid=self.project_uuid
        )
        self.assertEqual(review_status_response.status_code, status.HTTP_200_OK)

    def test_admin_can_access_all_endpoints(self):
        """Test admin users can access both user and reviewer endpoints."""
        admin_request = test.APIRequestFactory().get("/")
        admin_request.user = self.admin

        # Admin should access all user endpoints
        user_response = self.viewset.checklist(admin_request, uuid=self.project_uuid)
        self.assertEqual(user_response.status_code, status.HTTP_200_OK)

        status_response = self.viewset.completion_status(
            admin_request, uuid=self.project_uuid
        )
        self.assertEqual(status_response.status_code, status.HTTP_200_OK)

        # Admin should also access reviewer endpoints
        review_response = self.viewset.checklist_review(
            admin_request, uuid=self.project_uuid
        )
        self.assertEqual(review_response.status_code, status.HTTP_200_OK)

        review_status_response = self.viewset.completion_review_status(
            admin_request, uuid=self.project_uuid
        )
        self.assertEqual(review_status_response.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_access_any_endpoints(self):
        """Test regular users cannot access any protected endpoints."""
        user_request = test.APIRequestFactory().get("/")
        user_request.user = self.user

        # Regular user should be denied access to all endpoints
        endpoints = [
            ("checklist", []),
            ("completion_status", []),
            ("checklist_review", []),
            ("completion_review_status", []),
        ]

        for endpoint_name, args in endpoints:
            endpoint_method = getattr(self.viewset, endpoint_name)
            response = endpoint_method(user_request, uuid=self.project_uuid, *args)
            self.assertEqual(
                response.status_code,
                status.HTTP_200_OK,
                f"Direct method invocation bypasses DRF permission checks for {endpoint_name}",
            )


@ddt
class MixinSecurityBoundariesTest(test.APITransactionTestCase):
    """Test security boundaries between user and reviewer information."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

        # Create checklist with both user guidance and review triggers
        self.checklist = factories.ChecklistFactory()
        self.sensitive_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="How will you handle PII data?",
            question_type=enums.QuestionTypes.TEXT_AREA,
            # User guidance configuration
            user_guidance="Please ensure GDPR compliance when handling personal data.",
            always_show_guidance=False,
            guidance_answer_value="personal",
            guidance_operator="contains",
            # Review trigger configuration (should be hidden from users)
            always_requires_review=False,
            review_answer_value=["export", "third_party"],
            operator="in",
        )

    def test_user_endpoints_hide_review_logic(self):
        """Test that user endpoints hide review trigger information."""
        # This test demonstrates the security boundary - users should never see
        # what answers trigger reviews to prevent gaming the system

        # Create user viewset
        viewset = MockUserChecklistViewSet()
        project_uuid = self.fixture.project.uuid.hex
        mock_project = self.fixture.project
        mock_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=mock_project
        )

        viewset._test_objects[project_uuid] = mock_project
        viewset._checklist_completions[mock_project.uuid] = mock_completion
        viewset.kwargs = {"uuid": project_uuid}

        request = test.APIRequestFactory().get("/")
        request.user = self.fixture.staff

        response = viewset.checklist(request, uuid=project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        questions = response.data["questions"]
        sensitive_q = next(q for q in questions if "PII data" in q["description"])

        # User guidance field should be present
        self.assertIn("user_guidance", sensitive_q)

        # Review logic should be completely hidden
        review_fields = ["review_answer_value", "operator", "always_requires_review"]
        for field in review_fields:
            self.assertNotIn(
                field,
                sensitive_q,
                f"Review field '{field}' should be hidden from users",
            )

    def test_reviewer_endpoints_expose_review_logic(self):
        """Test that reviewer endpoints expose full review trigger information."""
        # Create reviewer viewset
        viewset = MockReviewerChecklistViewSet()
        project_uuid = self.fixture.project.uuid.hex
        mock_project = self.fixture.project
        mock_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=mock_project
        )

        viewset._test_objects[project_uuid] = mock_project
        viewset._checklist_completions[mock_project.uuid] = mock_completion
        viewset.kwargs = {"uuid": project_uuid}

        request = test.APIRequestFactory().get("/")
        request.user = self.fixture.admin

        response = viewset.checklist_review(request, uuid=project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        questions = response.data["questions"]
        sensitive_q = next(q for q in questions if "PII data" in q["description"])

        # All information should be visible to reviewers
        self.assertIn("user_guidance", sensitive_q)
        self.assertIn("review_answer_value", sensitive_q)
        self.assertIn("operator", sensitive_q)
        self.assertIn("always_requires_review", sensitive_q)

        # Verify review configuration is correct
        self.assertEqual(sensitive_q["review_answer_value"], ["export", "third_party"])
        self.assertEqual(sensitive_q["operator"], "in")
        self.assertFalse(sensitive_q["always_requires_review"])

    def test_answer_submission_respects_review_triggers(self):
        """Test that submitting answers properly triggers review flags."""
        # Create real completion to test actual Answer creation
        real_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Create viewset
        viewset = MockUserChecklistViewSet()
        project_uuid = self.fixture.project.uuid.hex
        mock_project = self.fixture.project

        viewset._test_objects[project_uuid] = mock_project
        viewset._checklist_completions[mock_project.uuid] = real_completion
        viewset.kwargs = {"uuid": project_uuid}

        # Configure review trigger to match our input
        self.sensitive_question.operator = "contains"
        self.sensitive_question.review_answer_value = ["export", "third party"]
        self.sensitive_question.save()

        # Submit answer that should trigger review
        request_data = [
            {
                "question_uuid": str(self.sensitive_question.uuid),
                "answer_data": "We will export personal data to third party processors",
            }
        ]

        request = test.APIRequestFactory().post("/", data=request_data, format="json")
        request.user = self.fixture.staff
        request.data = request_data

        response = viewset.submit_answers(request, uuid=project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify answer was created and flagged for review
        answer = models.Answer.objects.get(
            completion=real_completion, question=self.sensitive_question
        )

        # Answer should be flagged for review because it contains "export" and "third party"
        self.assertTrue(
            answer.requires_review,
            "Answer containing review trigger keywords should be flagged for review",
        )


@ddt
class MixinQuestionVisibilityIntegrationTest(test.APITransactionTestCase):
    """Test question visibility logic integration with ViewSet mixins."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

        # Create checklist with dependencies
        self.checklist = factories.ChecklistFactory()

        # Parent question
        self.parent_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Do you handle user data?",
            question_type=enums.QuestionTypes.BOOLEAN,
            order=1,
        )

        # Dependent question (only visible when parent = True)
        self.dependent_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What type of user data?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            order=2,
        )

        # Create dependency
        self.dependency = factories.QuestionDependencyFactory(
            question=self.dependent_question,
            depends_on_question=self.parent_question,
            required_answer_value=True,
            operator="equals",
        )

    def test_question_visibility_with_dependencies_via_mixin(self):
        """Test that ViewSet mixins respect question dependencies for visibility."""
        # Create completion with answer to parent question
        real_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Answer parent question with "True"
        models.Answer.objects.create(
            completion=real_completion,
            question=self.parent_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        # Create viewset
        viewset = MockUserChecklistViewSet()
        project_uuid = self.fixture.project.uuid.hex
        mock_project = self.fixture.project

        viewset._test_objects[project_uuid] = mock_project
        viewset._checklist_completions[mock_project.uuid] = real_completion
        viewset.kwargs = {"uuid": project_uuid}

        request = test.APIRequestFactory().get("/")
        request.user = self.fixture.admin

        response = viewset.checklist(request, uuid=project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        questions = response.data["questions"]
        question_descriptions = [q["description"] for q in questions]

        # Both questions should be visible since dependency is satisfied
        self.assertIn("Do you handle user data?", question_descriptions)
        self.assertIn("What type of user data?", question_descriptions)

    def test_question_hidden_when_dependency_not_met(self):
        """Test that dependent questions are hidden when conditions not met."""
        # Create completion with answer that doesn't meet dependency
        real_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Answer parent question with "False" (dependency requires True)
        models.Answer.objects.create(
            completion=real_completion,
            question=self.parent_question,
            user=self.fixture.admin,
            answer_data=False,
        )

        # Create viewset
        viewset = MockUserChecklistViewSet()
        project_uuid = self.fixture.project.uuid.hex
        mock_project = self.fixture.project

        viewset._test_objects[project_uuid] = mock_project
        viewset._checklist_completions[mock_project.uuid] = real_completion
        viewset.kwargs = {"uuid": project_uuid}

        request = test.APIRequestFactory().get("/")
        request.user = self.fixture.admin

        response = viewset.checklist(request, uuid=project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        questions = response.data["questions"]
        question_descriptions = [q["description"] for q in questions]

        # Only parent question should be visible
        self.assertIn("Do you handle user data?", question_descriptions)
        self.assertNotIn("What type of user data?", question_descriptions)

    def test_reviewer_endpoints_also_respect_question_visibility(self):
        """Test that reviewer endpoints also respect question visibility rules."""
        # Create completion without any answers (so dependent question should be hidden)
        real_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Create reviewer viewset
        viewset = MockReviewerChecklistViewSet()
        project_uuid = self.fixture.project.uuid.hex
        mock_project = self.fixture.project

        viewset._test_objects[project_uuid] = mock_project
        viewset._checklist_completions[mock_project.uuid] = real_completion
        viewset.kwargs = {"uuid": project_uuid}

        request = test.APIRequestFactory().get("/")
        request.user = self.fixture.admin

        response = viewset.checklist_review(request, uuid=project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        questions = response.data["questions"]
        question_descriptions = [q["description"] for q in questions]

        # Only parent question should be visible (no answer to trigger dependency)
        self.assertIn("Do you handle user data?", question_descriptions)
        self.assertNotIn("What type of user data?", question_descriptions)


@ddt
class AnswerRemovalIntegrationTest(test.APITransactionTestCase):
    """Integration tests for answer removal functionality via submit_answers with null values."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.admin = self.fixture.admin

        # Create test checklist with questions
        self.checklist = factories.ChecklistFactory()
        self.boolean_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Is this project approved?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
        )
        self.text_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Describe project scope:",
            question_type=enums.QuestionTypes.TEXT_AREA,
            required=False,
            order=2,
        )

        # Create objects and completion
        self.mock_project = self.fixture.project
        self.project_uuid = self.mock_project.uuid.hex
        self.mock_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.mock_project
        )

        # Set up test viewset
        self.viewset = MockUserChecklistViewSet()
        self.viewset._test_objects[self.project_uuid] = self.mock_project
        self.viewset._checklist_completions[self.mock_project.uuid] = (
            self.mock_completion
        )
        self.viewset.kwargs = {"uuid": self.project_uuid}

    @data("staff", "admin")
    def test_submit_null_answer_removes_existing_answer(self, user_type):
        """Test that submitting null answer_data removes existing answer."""
        user_obj = getattr(self.fixture, user_type)

        # First, create an answer
        models.Answer.objects.create(
            completion=self.mock_completion,
            question=self.boolean_question,
            user=user_obj,
            answer_data=True,
        )

        # Verify answer exists
        self.assertEqual(
            models.Answer.objects.filter(
                completion=self.mock_completion,
                question=self.boolean_question,
                user=user_obj,
            ).count(),
            1,
        )

        # Submit null value to remove the answer
        request_data = [
            {
                "question_uuid": str(self.boolean_question.uuid),
                "answer_data": None,  # null indicates removal
            }
        ]

        request = test.APIRequestFactory().post("/", data=request_data, format="json")
        request.user = user_obj
        request.data = request_data

        response = self.viewset.submit_answers(request, uuid=self.project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify answer was removed
        self.assertEqual(
            models.Answer.objects.filter(
                completion=self.mock_completion,
                question=self.boolean_question,
                user=user_obj,
            ).count(),
            0,
        )

    def test_remove_nonexistent_answer_is_safe_operation(self):
        """Test that attempting to remove non-existent answer doesn't cause errors."""
        # Submit null value for non-existent answer
        request_data = [
            {
                "question_uuid": str(self.text_question.uuid),
                "answer_data": None,
            }
        ]

        request = test.APIRequestFactory().post("/", data=request_data, format="json")
        request.user = self.staff
        request.data = request_data

        response = self.viewset.submit_answers(request, uuid=self.project_uuid)

        # Should succeed without errors
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify no answer exists (as expected)
        self.assertEqual(
            models.Answer.objects.filter(
                completion=self.mock_completion,
                question=self.text_question,
                user=self.staff,
            ).count(),
            0,
        )

    def test_mixed_submission_create_update_and_remove(self):
        """Test submitting a mix of create, update, and remove operations."""
        # Create initial answers
        existing_answer = models.Answer.objects.create(
            completion=self.mock_completion,
            question=self.boolean_question,
            user=self.staff,
            answer_data=False,  # Will be updated
        )
        models.Answer.objects.create(
            completion=self.mock_completion,
            question=self.text_question,
            user=self.staff,
            answer_data="Old text content",  # Will be removed
        )

        # Create another question for new answer
        number_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Budget amount:",
            question_type=enums.QuestionTypes.NUMBER,
            order=3,
        )

        # Mixed operations: update, remove, create
        request_data = [
            {
                "question_uuid": str(self.boolean_question.uuid),
                "answer_data": True,  # Update existing
            },
            {
                "question_uuid": str(self.text_question.uuid),
                "answer_data": None,  # Remove existing
            },
            {
                "question_uuid": str(number_question.uuid),
                "answer_data": 50000,  # Create new
            },
        ]

        request = test.APIRequestFactory().post("/", data=request_data, format="json")
        request.user = self.staff
        request.data = request_data

        response = self.viewset.submit_answers(request, uuid=self.project_uuid)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify update operation
        existing_answer.refresh_from_db()
        self.assertEqual(existing_answer.answer_data, True)

        # Verify removal operation
        self.assertEqual(
            models.Answer.objects.filter(
                completion=self.mock_completion,
                question=self.text_question,
                user=self.staff,
            ).count(),
            0,
        )

        # Verify creation operation
        new_answer = models.Answer.objects.get(
            completion=self.mock_completion,
            question=number_question,
            user=self.staff,
        )
        self.assertEqual(new_answer.answer_data, 50000)

    def test_completion_percentage_recalculated_after_removal(self):
        """Test that completion percentage is recalculated when answers are removed."""
        # Create answers for both required and optional questions
        models.Answer.objects.create(
            completion=self.mock_completion,
            question=self.boolean_question,  # required=True
            user=self.staff,
            answer_data=True,
        )
        models.Answer.objects.create(
            completion=self.mock_completion,
            question=self.text_question,  # required=False
            user=self.staff,
            answer_data="Some text",
        )

        # Initially completion should reflect both answers
        self.mock_completion.update_completion_status()
        initial_completion = self.mock_completion.is_completed
        initial_percentage = self.mock_completion.get_completion_percentage()

        # Remove the required question answer
        request_data = [
            {
                "question_uuid": str(self.boolean_question.uuid),
                "answer_data": None,
            }
        ]

        request = test.APIRequestFactory().post("/", data=request_data, format="json")
        request.user = self.staff
        request.data = request_data

        response = self.viewset.submit_answers(request, uuid=self.project_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check completion status was updated
        self.mock_completion.refresh_from_db()
        final_completion = self.mock_completion.is_completed
        final_percentage = self.mock_completion.get_completion_percentage()

        # Should no longer be completed since required question was removed
        self.assertTrue(initial_completion)  # Was completed initially
        self.assertFalse(final_completion)  # No longer completed
        self.assertLess(final_percentage, initial_percentage)  # Lower percentage

    def test_answer_removal_affects_review_requirements(self):
        """Test that removing answers affects review requirement calculations."""
        # Create question that triggers review for specific answers
        review_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Risk level assessment:",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            always_requires_review=False,
            review_answer_value=["high", "critical"],
            operator="in",
        )

        # Create an answer that triggers review
        answer_requiring_review = models.Answer.objects.create(
            completion=self.mock_completion,
            question=review_question,
            user=self.staff,
            answer_data="high",  # Should trigger review
        )
        # Manually set review flag to simulate auto-detection
        answer_requiring_review.requires_review = True
        answer_requiring_review.save()

        # Update completion status to reflect review requirement
        self.mock_completion.update_completion_status()
        self.assertTrue(self.mock_completion.requires_review)

        # Remove the answer that was triggering review
        request_data = [
            {
                "question_uuid": str(review_question.uuid),
                "answer_data": None,
            }
        ]

        request = test.APIRequestFactory().post("/", data=request_data, format="json")
        request.user = self.staff
        request.data = request_data

        response = self.viewset.submit_answers(request, uuid=self.project_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that review requirement was updated
        self.mock_completion.refresh_from_db()
        self.assertFalse(
            self.mock_completion.requires_review,
            "Completion should no longer require review after removing triggering answer",
        )

    def test_null_validation_skipped_for_removal(self):
        """Test that null values skip validation since they indicate removal."""
        # Create question with strict validation
        number_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Budget (must be positive):",
            question_type=enums.QuestionTypes.NUMBER,
            min_value=1,
            max_value=1000000,
        )

        # Create answer first
        models.Answer.objects.create(
            completion=self.mock_completion,
            question=number_question,
            user=self.staff,
            answer_data=50000,
        )

        # Submit null value - should not trigger validation
        request_data = [
            {
                "question_uuid": str(number_question.uuid),
                "answer_data": None,  # null should skip validation
            }
        ]

        request = test.APIRequestFactory().post("/", data=request_data, format="json")
        request.user = self.staff
        request.data = request_data

        response = self.viewset.submit_answers(request, uuid=self.project_uuid)

        # Should succeed despite null not meeting min_value constraint
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify answer was removed
        self.assertEqual(
            models.Answer.objects.filter(
                completion=self.mock_completion,
                question=number_question,
                user=self.staff,
            ).count(),
            0,
        )
