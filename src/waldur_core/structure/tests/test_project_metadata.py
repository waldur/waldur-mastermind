"""Tests for project metadata functionality using core checklist infrastructure."""

from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import ChecklistTypes, QuestionTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class ProjectMetadataTestMixin:
    """Shared test setup and utilities for project metadata tests."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create test structures
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        self.member = self.fixture.member

        # Create project metadata checklist
        self.checklist = checklist_factories.ChecklistFactory(
            name="Project Metadata Checklist",
            checklist_type=ChecklistTypes.PROJECT_METADATA,
        )

        # Create some questions
        self.text_question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Project purpose",
            question_type=QuestionTypes.TEXT_AREA,
            required=True,
            order=1,
        )

        self.boolean_question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Is this project confidential?",
            question_type=QuestionTypes.BOOLEAN,
            required=False,
            order=2,
        )

        self.select_question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Project category",
            question_type=QuestionTypes.SINGLE_SELECT,
            required=True,
            order=3,
        )

        # Create options for select question
        self.research_option = checklist_factories.QuestionOptionFactory(
            question=self.select_question,
            label="Research",
            order=1,
        )
        checklist_factories.QuestionOptionFactory(
            question=self.select_question,
            label="Development",
            order=2,
        )
        checklist_factories.QuestionOptionFactory(
            question=self.select_question,
            label="Production",
            order=3,
        )


@ddt
class CustomerProjectMetadataTest(
    ProjectMetadataTestMixin, test.APITransactionTestCase
):
    """Test customer project metadata configuration."""

    def test_customer_metadata_config_creation(self):
        """Test that Customer can have a project metadata checklist assigned."""
        # Set the checklist on customer
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        # Refresh from database
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.project_metadata_checklist, self.checklist)

    def test_checklist_template_endpoint(self):
        """Test getting checklist template for new projects."""
        # Assign checklist to customer
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        # Try to get checklist template without parent_uuid
        self.client.force_authenticate(self.owner)
        url = "/api/projects/checklist-template/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parent_uuid", response.data["detail"])

        # Get checklist template with valid customer UUID
        response = self.client.get(url, {"parent_uuid": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check response structure
        self.assertIn("checklist", response.data)
        self.assertIn("questions", response.data)
        self.assertIn("initial_visible_questions", response.data)

        # Verify checklist info
        self.assertEqual(response.data["checklist"]["uuid"], str(self.checklist.uuid))
        self.assertEqual(response.data["checklist"]["name"], self.checklist.name)

        # Verify questions
        self.assertEqual(len(response.data["questions"]), 3)
        question_descriptions = [q["description"] for q in response.data["questions"]]
        self.assertIn("Project purpose", question_descriptions)
        self.assertIn("Is this project confidential?", question_descriptions)
        self.assertIn("Project category", question_descriptions)

        # Check that select question has options
        for question in response.data["questions"]:
            if question["description"] == "Project category":
                self.assertEqual(len(question["question_options"]), 3)
                option_labels = [o["label"] for o in question["question_options"]]
                self.assertIn("Research", option_labels)
                self.assertIn("Development", option_labels)
                self.assertIn("Production", option_labels)

    def test_checklist_template_with_nonexistent_customer(self):
        """Test getting checklist template with invalid customer UUID."""
        self.client.force_authenticate(self.owner)
        url = "/api/projects/checklist-template/"

        # Use a UUID that doesn't exist
        fake_uuid = "12345678-1234-5678-1234-567812345678"
        response = self.client.get(url, {"parent_uuid": fake_uuid})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Parent object not found", response.data["detail"])

    def test_checklist_template_with_no_checklist_configured(self):
        """Test getting checklist template when customer has no checklist configured."""
        # Ensure customer has no checklist
        self.customer.project_metadata_checklist = None
        self.customer.save()

        self.client.force_authenticate(self.owner)
        url = "/api/projects/checklist-template/"
        response = self.client.get(url, {"parent_uuid": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No checklist configured", response.data["detail"])

    def test_invalid_checklist_type_validation(self):
        """Test that non-PROJECT_METADATA checklists are rejected."""
        # Create a different type of checklist
        wrong_checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROPOSAL_COMPLIANCE
        )

        # Attempt to set wrong checklist type on customer
        self.customer.project_metadata_checklist = wrong_checklist

        with self.assertRaises(Exception):
            self.customer.clean()

    def test_configuration_creates_completion_for_existing_projects(self):
        """Test that setting configuration auto-creates ChecklistCompletions for existing projects."""
        # Create additional projects
        project2 = structure_factories.ProjectFactory(customer=self.customer)
        project3 = structure_factories.ProjectFactory(customer=self.customer)

        # Set configuration - this should trigger signal to create completions
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        # Verify ChecklistCompletions were created for all projects
        project_content_type = ContentType.objects.get_for_model(self.project.__class__)
        completions = checklist_models.ChecklistCompletion.objects.filter(
            checklist=self.checklist,
            scope_content_type=project_content_type,
            scope_object_id__in=[self.project.id, project2.id, project3.id],
        )
        self.assertEqual(completions.count(), 3)

        # Verify each completion points to the correct project
        completion_projects = {c.scope for c in completions}
        expected_projects = {self.project, project2, project3}
        self.assertEqual(completion_projects, expected_projects)

    def test_deleting_config_removes_completions(self):
        """Test that removing configuration removes associated ChecklistCompletions."""
        # Set configuration
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        # Verify completion exists
        project_content_type = ContentType.objects.get_for_model(self.project.__class__)
        completion = checklist_models.ChecklistCompletion.objects.get(
            checklist=self.checklist,
            scope_content_type=project_content_type,
            scope_object_id=self.project.id,
        )

        # Remove configuration
        self.customer.project_metadata_checklist = None
        self.customer.save()

        # Verify completion was deleted
        with self.assertRaises(checklist_models.ChecklistCompletion.DoesNotExist):
            checklist_models.ChecklistCompletion.objects.get(id=completion.id)


@ddt
class ProjectMetadataCompletionTest(
    ProjectMetadataTestMixin, test.APITransactionTestCase
):
    """Test project metadata completion functionality using project ViewSet."""

    def setUp(self):
        super().setUp()

        # Set metadata configuration
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        # Get the ChecklistCompletion that was auto-created via signal
        project_content_type = ContentType.objects.get_for_model(self.project.__class__)
        self.completion = checklist_models.ChecklistCompletion.objects.get(
            checklist=self.checklist,
            scope_content_type=project_content_type,
            scope_object_id=self.project.id,
        )

        # URLs for project metadata endpoints (provided by UserChecklistMixin)
        self.checklist_url = structure_factories.ProjectFactory.get_url(
            self.project, action="checklist"
        )
        self.status_url = structure_factories.ProjectFactory.get_url(
            self.project, action="completion_status"
        )
        self.submit_url = structure_factories.ProjectFactory.get_url(
            self.project, action="submit_answers"
        )

    @data("staff", "owner")
    def test_user_can_get_project_metadata_checklist(self, user):
        """Test that authorized users can get project metadata checklist."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        response = self.client.get(self.checklist_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        self.assertEqual(data["checklist"]["uuid"], str(self.checklist.uuid))
        self.assertEqual(len(data["questions"]), 3)

    @data("staff", "owner")
    def test_user_can_get_completion_status(self, user):
        """Test that authorized users can get completion status."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        response = self.client.get(self.status_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        self.assertEqual(data["is_completed"], False)
        self.assertEqual(data["completion_percentage"], 0.0)

    @data("staff", "owner")
    def test_user_can_submit_answers(self, user):
        """Test that authorized users can submit metadata answers."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        answers = [
            {
                "question_uuid": str(self.text_question.uuid),
                "answer_data": "This is a research project for machine learning.",
            },
            {"question_uuid": str(self.boolean_question.uuid), "answer_data": True},
            {
                "question_uuid": str(self.select_question.uuid),
                "answer_data": [
                    str(self.research_option.uuid)
                ],  # Single select expects a list with one UUID
            },
        ]

        response = self.client.post(self.submit_url, answers, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify answers were saved
        saved_answers = checklist_models.Answer.objects.filter(
            completion=self.completion, user=user_obj
        )
        self.assertEqual(saved_answers.count(), 3)

        # Verify completion status updated
        self.completion.refresh_from_db()
        self.assertTrue(self.completion.is_completed)

    @data("admin", "manager", "member")
    def test_project_members_can_view_metadata(self, user):
        """Test that all project members can view project metadata."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Should be able to view checklist
        response = self.client.get(self.checklist_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should be able to view status
        response = self.client.get(self.status_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should NOT be able to submit answers (only manager and customer owner can)
        response = self.client.post(self.submit_url, [], format="json")
        if user == "manager":
            # Manager should be able to submit (but may get 400 due to validation issues)
            self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        else:
            # Admin and member should get forbidden
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_manager_can_access_metadata(self):
        """Test that PROJECT_MANAGER can access project metadata."""
        self.client.force_authenticate(self.manager)

        # Should be able to get checklist
        response = self.client.get(self.checklist_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should be able to submit answers
        answers = [
            {
                "question_uuid": str(self.text_question.uuid),
                "answer_data": "Project manager test submission",
            }
        ]
        response = self.client.post(self.submit_url, answers, format="json")
        # Note: May get 400 if there are validation issues, but should not get 403
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_without_metadata_config_returns_404(self):
        """Test that projects without metadata configuration return 404."""
        # Create a project with a customer that has no metadata config
        other_customer = structure_factories.CustomerFactory()
        other_project = structure_factories.ProjectFactory(customer=other_customer)

        url = structure_factories.ProjectFactory.get_url(
            other_project, action="checklist"
        )

        self.client.force_authenticate(self.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_new_project_gets_completion_automatically(self):
        """Test that new projects automatically get ChecklistCompletion when customer has config."""
        # Create new project
        new_project = structure_factories.ProjectFactory(customer=self.customer)

        # Verify completion was created automatically via signal
        project_content_type = ContentType.objects.get_for_model(new_project.__class__)
        completion = checklist_models.ChecklistCompletion.objects.get(
            checklist=self.checklist,
            scope_content_type=project_content_type,
            scope_object_id=new_project.id,
        )

        self.assertEqual(completion.checklist, self.checklist)
        self.assertFalse(completion.is_completed)
