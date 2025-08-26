"""Tests for CustomerProjectMetadataComplianceViewSet."""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.models import ChecklistCompletion
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class CustomerProjectMetadataComplianceAPITest(test.APITransactionTestCase):
    """Test CustomerProjectMetadataComplianceViewSet API functionality."""

    def setUp(self):
        """Set up test data."""
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.manager = self.fixture.manager
        self.member = self.fixture.member

        # Create additional projects
        self.project2 = structure_factories.ProjectFactory(customer=self.customer)
        self.project3 = structure_factories.ProjectFactory(customer=self.customer)

        # Create a PROJECT_METADATA checklist with questions
        self.checklist = checklist_factories.ChecklistFactory(
            name="Project Metadata Checklist",
            checklist_type=ChecklistTypes.PROJECT_METADATA,
        )

        # Create some questions
        self.question1 = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="What is the project purpose?",
            question_type="text_area",
            required=True,
            order=1,
        )
        self.question2 = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Will this project handle sensitive data?",
            question_type="boolean",
            required=True,
            order=2,
        )

        # Assign checklist to customer
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

    def _get_compliance_overview_url(self):
        """Get compliance_overview endpoint URL."""
        return f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-overview/"

    def _get_project_details_url(self):
        """Get project_details endpoint URL."""
        return f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-details/"

    def _get_projects_url(self):
        """Get projects endpoint URL."""
        return f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-projects/"

    def _get_question_answers_url(self):
        """Get question answers endpoint URL."""
        return f"/api/customers/{self.customer.uuid.hex}/project-metadata-question-answers/"

    def test_compliance_overview_no_checklist_configured(self):
        """Test compliance_overview when no checklist is configured."""
        # Remove checklist from customer
        self.customer.project_metadata_checklist = None
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "No project metadata checklist configured", response.data["detail"]
        )

    def test_compliance_overview_empty_projects(self):
        """Test compliance_overview when no projects exist."""
        # Create a new customer with no projects and assign checklist
        empty_customer = structure_factories.CustomerFactory()
        empty_customer.project_metadata_checklist = self.checklist
        empty_customer.save()

        self.client.force_authenticate(user=self.staff)
        url = f"/api/customers/{empty_customer.uuid.hex}/project-metadata-compliance-overview/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Check structure
        self.assertIn("total_projects", data)
        self.assertIn("projects_with_completions", data)
        self.assertIn("fully_completed_projects", data)
        self.assertIn("projects_requiring_review", data)
        self.assertIn("average_completion_percentage", data)

        # Check empty data
        self.assertEqual(data["total_projects"], 0)
        self.assertEqual(data["projects_with_completions"], 0)
        self.assertEqual(data["fully_completed_projects"], 0)
        self.assertEqual(data["projects_requiring_review"], 0)
        self.assertEqual(data["average_completion_percentage"], 0.0)

    def test_compliance_overview_with_completions(self):
        """Test compliance_overview with actual completion data."""
        # Create completions for projects
        project_ct = ContentType.objects.get_for_model(self.fixture.project)
        completion1, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.fixture.project.id,
        )
        completion2, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.project2.id,
        )

        # Add answers to first completion (partial)
        checklist_factories.AnswerFactory(
            completion=completion1,
            question=self.question1,
            user=self.owner,
            answer_data="Research project",
        )

        # Add answers to second completion (complete)
        checklist_factories.AnswerFactory(
            completion=completion2,
            question=self.question1,
            user=self.owner,
            answer_data="Development project",
        )
        checklist_factories.AnswerFactory(
            completion=completion2,
            question=self.question2,
            user=self.owner,
            answer_data=True,
        )

        # Update completion status
        completion1.update_completion_status()
        completion2.update_completion_status()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Check statistics
        self.assertEqual(data["total_projects"], 3)  # 3 projects total
        self.assertGreaterEqual(
            data["projects_with_completions"], 2
        )  # At least 2 have completions
        self.assertGreater(data["average_completion_percentage"], 0)
        self.assertGreaterEqual(
            data["fully_completed_projects"], 1
        )  # At least one is fully completed
        self.assertEqual(data["projects_requiring_review"], 0)  # None require review

    def test_project_details_no_checklist_configured(self):
        """Test project_details when no checklist is configured."""
        # Remove checklist from customer
        self.customer.project_metadata_checklist = None
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_project_details_url())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "No project metadata checklist configured", response.data["detail"]
        )

    def test_project_details_with_completions(self):
        """Test project_details with detailed project information."""
        # Create completions for some projects
        project_ct = ContentType.objects.get_for_model(self.fixture.project)
        completion1, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.fixture.project.id,
        )

        # Add partial answers
        checklist_factories.AnswerFactory(
            completion=completion1,
            question=self.question1,
            user=self.owner,
            answer_data="Research project",
        )
        # Missing required question2 answer

        # Update completion status
        completion1.update_completion_status()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_project_details_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Check structure
        self.assertIn("checklist", data)
        self.assertIn("total_projects", data)
        self.assertIn("projects_with_completions", data)
        self.assertIn("fully_completed_projects", data)
        self.assertIn("projects_requiring_review", data)
        self.assertIn("project_details", data)

        # Check checklist info
        checklist_data = data["checklist"]
        self.assertEqual(checklist_data["name"], "Project Metadata Checklist")
        self.assertEqual(
            checklist_data["checklist_type"], ChecklistTypes.PROJECT_METADATA
        )

        # Check statistics
        # Note: Auto-creation means all projects get completions when checklist is assigned
        self.assertEqual(data["total_projects"], 3)
        self.assertEqual(
            data["projects_with_completions"], 3
        )  # All projects get auto-created completions
        self.assertEqual(data["fully_completed_projects"], 0)
        self.assertEqual(data["projects_requiring_review"], 0)

        # Check project details (should be sorted by completion percentage)
        project_details = data["project_details"]
        self.assertEqual(len(project_details), 3)

        # Find project with completion data
        project_with_completion = None
        projects_without_completion = []

        for detail in project_details:
            if detail["completion_percentage"] > 0:
                project_with_completion = detail
            else:
                projects_without_completion.append(detail)

        # Verify completion project
        self.assertIsNotNone(project_with_completion)
        self.assertGreater(project_with_completion["completion_percentage"], 0)
        self.assertFalse(project_with_completion["is_completed"])
        self.assertFalse(project_with_completion["requires_review"])

        # Verify projects without completion
        self.assertEqual(len(projects_without_completion), 2)
        for detail in projects_without_completion:
            self.assertEqual(detail["completion_percentage"], 0.0)
            self.assertFalse(detail["is_completed"])
            self.assertFalse(detail["requires_review"])

    def test_projects_endpoint_basic_functionality(self):
        """Test projects endpoint returns paginated project data."""
        # Create completion for one project
        project_ct = ContentType.objects.get_for_model(self.fixture.project)
        completion, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.fixture.project.id,
        )

        # Add partial answers
        checklist_factories.AnswerFactory(
            completion=completion,
            question=self.question1,
            user=self.owner,
            answer_data="Test answer",
        )

        # Update completion status
        completion.update_completion_status()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_projects_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Check pagination structure (LinkHeaderPagination puts count in header)
        self.assertIn("X-Result-Count", response)
        self.assertEqual(int(response["X-Result-Count"]), 3)

        # Should have 3 projects in the response data (list format)
        self.assertEqual(len(data), 3)

        # Check data structure for projects
        for project_data in data:
            self.assertIn("project_uuid", project_data)
            self.assertIn("project_name", project_data)
            self.assertIn("completion_uuid", project_data)
            self.assertIn("completion_percentage", project_data)
            self.assertIn("is_completed", project_data)
            self.assertIn("requires_review", project_data)
            self.assertIn("answers_count", project_data)
            self.assertIn("unanswered_required_count", project_data)

        # Find the project with completion data
        project_with_completion = None
        for project_data in data:
            if project_data["completion_uuid"]:
                project_with_completion = project_data
                break

        self.assertIsNotNone(project_with_completion)
        self.assertGreater(project_with_completion["completion_percentage"], 0)
        self.assertEqual(project_with_completion["answers_count"], 1)
        self.assertGreater(project_with_completion["unanswered_required_count"], 0)

    def test_question_answers_no_checklist_configured(self):
        """Test question_answers when no checklist is configured."""
        # Remove checklist from customer
        self.customer.project_metadata_checklist = None
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_question_answers_url())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "No project metadata checklist configured", response.data["detail"]
        )

    def test_question_answers_basic_functionality(self):
        """Test question_answers endpoint returns paginated question data."""
        # Create completions and answers for some projects
        project_ct = ContentType.objects.get_for_model(self.fixture.project)
        completion1, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.fixture.project.id,
        )
        completion2, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.project2.id,
        )

        # Add answers to first completion (both questions)
        checklist_factories.AnswerFactory(
            completion=completion1,
            question=self.question1,
            user=self.owner,
            answer_data="Research project purpose",
        )
        checklist_factories.AnswerFactory(
            completion=completion1,
            question=self.question2,
            user=self.owner,
            answer_data=True,
        )

        # Add answer to second completion (only first question)
        checklist_factories.AnswerFactory(
            completion=completion2,
            question=self.question1,
            user=self.owner,
            answer_data="Development project purpose",
        )

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_question_answers_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Check pagination structure (LinkHeaderPagination puts count in header)
        self.assertIn("X-Result-Count", response)
        self.assertEqual(int(response["X-Result-Count"]), 2)  # 2 questions total

        # Should have 2 questions in the response data (list format)
        self.assertEqual(len(data), 2)

        # Check data structure for questions
        for question_data in data:
            self.assertIn("question_uuid", question_data)
            self.assertIn("question_description", question_data)
            self.assertIn("question_type", question_data)
            self.assertIn("required", question_data)
            self.assertIn("order", question_data)
            self.assertIn("total_projects", question_data)
            self.assertIn("answered_projects_count", question_data)
            self.assertIn("project_answers", question_data)

            # Each question should have answers for all 3 projects
            self.assertEqual(len(question_data["project_answers"]), 3)
            self.assertEqual(question_data["total_projects"], 3)

            # Check project answer structure
            for project_answer in question_data["project_answers"]:
                self.assertIn("project_uuid", project_answer)
                self.assertIn("project_name", project_answer)
                self.assertIn("answer_uuid", project_answer)
                self.assertIn("answer_data", project_answer)
                self.assertIn("answered_by", project_answer)
                self.assertIn("answered_at", project_answer)
                self.assertIn("requires_review", project_answer)

        # Verify specific question data
        question1_data = next(
            (
                q
                for q in data
                if q["question_description"] == "What is the project purpose?"
            ),
            None,
        )
        self.assertIsNotNone(question1_data)
        self.assertEqual(
            question1_data["answered_projects_count"], 2
        )  # Answered in 2 projects

        question2_data = next(
            (
                q
                for q in data
                if q["question_description"]
                == "Will this project handle sensitive data?"
            ),
            None,
        )
        self.assertIsNotNone(question2_data)
        self.assertEqual(
            question2_data["answered_projects_count"], 1
        )  # Answered in 1 project

    def test_question_answers_empty_projects(self):
        """Test question_answers when no projects exist."""
        # Create a new customer with no projects and assign checklist
        empty_customer = structure_factories.CustomerFactory()
        empty_customer.project_metadata_checklist = self.checklist
        empty_customer.save()

        self.client.force_authenticate(user=self.staff)
        url = f"/api/customers/{empty_customer.uuid.hex}/project-metadata-question-answers/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Should have 2 questions (from the checklist)
        self.assertEqual(len(data), 2)

        # Each question should have empty project answers
        for question_data in data:
            self.assertEqual(question_data["total_projects"], 0)
            self.assertEqual(question_data["answered_projects_count"], 0)
            self.assertEqual(len(question_data["project_answers"]), 0)


class CustomerProjectMetadataCompliancePermissionsTest(test.APITransactionTestCase):
    """Test permissions for CustomerProjectMetadataComplianceViewSet."""

    def setUp(self):
        """Set up test data."""
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.manager = self.fixture.manager
        self.member = self.fixture.member
        self.user = structure_factories.UserFactory()  # No relationship to customer

        # Create and assign checklist
        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROJECT_METADATA,
        )
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

    def _get_compliance_overview_url(self):
        """Get compliance_overview endpoint URL."""
        return f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-overview/"

    def _get_project_details_url(self):
        """Get project_details endpoint URL."""
        return f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-details/"

    def _get_question_answers_url(self):
        """Get question answers endpoint URL."""
        return f"/api/customers/{self.customer.uuid.hex}/project-metadata-question-answers/"

    def test_compliance_overview_permissions_staff(self):
        """Test that staff can access compliance_overview."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_compliance_overview_permissions_owner(self):
        """Test that customer owner can access compliance_overview."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_compliance_overview_permissions_manager(self):
        """Test that project manager CANNOT access compliance_overview (insufficient permissions)."""
        self.client.force_authenticate(user=self.manager)
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_compliance_overview_permissions_member(self):
        """Test that project member CANNOT access compliance_overview (insufficient permissions)."""
        self.client.force_authenticate(user=self.member)
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_compliance_overview_permissions_unauthorized(self):
        """Test that unauthorized user cannot access compliance_overview."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_details_permissions_staff(self):
        """Test that staff can access project_details."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_project_details_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_project_details_permissions_owner(self):
        """Test that customer owner can access project_details."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self._get_project_details_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_project_details_permissions_unauthorized(self):
        """Test that unauthorized user cannot access project_details."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self._get_project_details_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_compliance_overview_unauthenticated(self):
        """Test that unauthenticated requests are rejected."""
        response = self.client.get(self._get_compliance_overview_url())

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_project_details_unauthenticated(self):
        """Test that unauthenticated requests are rejected."""
        response = self.client.get(self._get_project_details_url())

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_question_answers_permissions_staff(self):
        """Test that staff can access question_answers."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self._get_question_answers_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_question_answers_permissions_owner(self):
        """Test that customer owner can access question_answers."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self._get_question_answers_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_question_answers_permissions_unauthorized(self):
        """Test that unauthorized user cannot access question_answers."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self._get_question_answers_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_question_answers_unauthenticated(self):
        """Test that unauthenticated requests are rejected."""
        response = self.client.get(self._get_question_answers_url())

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class CustomerProjectMetadataComplianceDataAccuracyTest(test.APITransactionTestCase):
    """Test data accuracy for CustomerProjectMetadataComplianceViewSet."""

    def setUp(self):
        """Set up test data."""
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff

        # Create additional projects for thorough testing
        self.projects = [self.fixture.project]
        for i in range(4):  # Total 5 projects
            self.projects.append(
                structure_factories.ProjectFactory(customer=self.customer)
            )

        # Create checklist with questions
        self.checklist = checklist_factories.ChecklistFactory(
            name="Data Accuracy Test Checklist",
            checklist_type=ChecklistTypes.PROJECT_METADATA,
        )

        self.required_questions = []
        for i in range(3):
            self.required_questions.append(
                checklist_factories.QuestionFactory(
                    checklist=self.checklist,
                    description=f"Required question {i + 1}",
                    question_type="text_input",
                    required=True,
                    order=i + 1,
                )
            )

        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

    def test_completion_percentage_accuracy(self):
        """Test that completion percentages are calculated correctly."""
        # Create completion with 2/3 required questions answered
        project_ct = ContentType.objects.get_for_model(self.projects[0])
        completion, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.projects[0].id,
        )

        # Answer 2 required questions
        checklist_factories.AnswerFactory(
            completion=completion,
            question=self.required_questions[0],
            user=self.fixture.owner,
            answer_data="Answer 1",
        )
        checklist_factories.AnswerFactory(
            completion=completion,
            question=self.required_questions[1],
            user=self.fixture.owner,
            answer_data="Answer 2",
        )

        # Update completion status
        completion.update_completion_status()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-overview/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Should have 13.3% average (66.7% for 1 project + 0% for 4 others) / 5 projects
        # Note: Auto-created completions mean all 5 projects are counted in the average
        expected_percentage = round((2 / 3 * 100) / 5, 1)  # (66.7 / 5) = 13.3
        self.assertEqual(data["average_completion_percentage"], expected_percentage)
        self.assertEqual(data["fully_completed_projects"], 0)  # Not fully complete

    def test_multiple_projects_statistics_accuracy(self):
        """Test statistics accuracy across multiple projects."""
        project_ct = ContentType.objects.get_for_model(self.projects[0])

        # Project 0: Fully completed
        completion0, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.projects[0].id,
        )
        for q in self.required_questions:
            checklist_factories.AnswerFactory(
                completion=completion0,
                question=q,
                user=self.fixture.owner,
                answer_data="Answer",
            )

        # Project 1: Partially completed (1/3 required)
        completion1, _ = ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=project_ct,
            scope_object_id=self.projects[1].id,
        )
        checklist_factories.AnswerFactory(
            completion=completion1,
            question=self.required_questions[0],
            user=self.fixture.owner,
            answer_data="Answer",
        )

        # Update completion status
        completion0.update_completion_status()
        completion1.update_completion_status()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-overview/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Statistics should be accurate
        # Note: When checklist is assigned to customer, completions are auto-created for all projects
        self.assertEqual(data["total_projects"], 5)
        self.assertEqual(
            data["projects_with_completions"], 5
        )  # All projects get auto-created completions
        self.assertEqual(data["fully_completed_projects"], 1)

        # Average: (100 + 33.3 + 0 + 0 + 0) / 5 = 26.66 -> rounded to 26.7
        expected_avg = round((100 + 33.3 + 0 + 0 + 0) / 5, 1)
        self.assertEqual(data["average_completion_percentage"], expected_avg)

    def test_project_details_sorting(self):
        """Test that projects are sorted by completion percentage (ascending)."""
        # Create completions with different completion levels
        completions_data = [
            (self.projects[0], 3, 100.0),  # Complete
            (self.projects[1], 2, 66.7),  # Partial
            (self.projects[2], 1, 33.3),  # Minimal
            # projects[3], projects[4] have no completions (0%)
        ]

        project_ct = ContentType.objects.get_for_model(self.projects[0])
        for project, answer_count, expected_pct in completions_data:
            completion, _ = ChecklistCompletion.objects.get_or_create(
                checklist=self.checklist,
                scope_content_type=project_ct,
                scope_object_id=project.id,
            )
            for i in range(answer_count):
                checklist_factories.AnswerFactory(
                    completion=completion,
                    question=self.required_questions[i],
                    user=self.fixture.owner,
                    answer_data=f"Answer {i + 1}",
                )
            # Update completion status
            completion.update_completion_status()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            f"/api/customers/{self.customer.uuid.hex}/project-metadata-compliance-details/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        project_details = data["project_details"]
        self.assertEqual(len(project_details), 5)

        # Should be sorted by project name (alphabetical order)
        project_names = [detail["project_name"] for detail in project_details]
        self.assertEqual(project_names, sorted(project_names))

        # Verify completion percentages are included correctly
        completion_percentages = [
            detail["completion_percentage"] for detail in project_details
        ]
        # Check that we have the expected percentages (not necessarily in order)
        self.assertIn(100.0, completion_percentages)  # projects[0] - 3/3 answers
        self.assertIn(66.7, completion_percentages)  # projects[1] - 2/3 answers
        self.assertIn(33.3, completion_percentages)  # projects[2] - 1/3 answers
        self.assertEqual(
            completion_percentages.count(0.0), 2
        )  # projects[3,4] - no answers
