from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import ChecklistTypes, QuestionTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories, fixtures


class ProjectMetadataExposureTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROJECT_METADATA
        )
        self.text_question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            question_type=QuestionTypes.TEXT_INPUT,
            description="Call ID",
            order=1,
        )
        self.select_question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            question_type=QuestionTypes.SINGLE_SELECT,
            description="Project type",
            order=2,
        )
        self.option = checklist_factories.QuestionOptionFactory(
            question=self.select_question, label="Academic"
        )

        self.project.customer.project_metadata_checklist = self.checklist
        self.project.customer.save()

        # Setting the checklist on the customer backfills a completion for the
        # existing project; reuse it (the (project, checklist) pair is unique).
        self.completion, _ = checklist_models.ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist,
            scope_content_type=ContentType.objects.get_for_model(type(self.project)),
            scope_object_id=self.project.id,
        )
        checklist_factories.AnswerFactory(
            completion=self.completion,
            question=self.text_question,
            answer_data="EXT-2026-042",
            user=self.fixture.owner,
        )
        checklist_factories.AnswerFactory(
            completion=self.completion,
            question=self.select_question,
            answer_data=[str(self.option.uuid)],
            user=self.fixture.owner,
        )

        self.url = reverse("project-detail", kwargs={"uuid": self.project.uuid})

    def test_owner_sees_metadata_answers(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        metadata = response.data["project_metadata"]
        # Ordered by question order
        self.assertEqual([m["question"] for m in metadata], ["Call ID", "Project type"])

        call_id = metadata[0]
        self.assertEqual(call_id["question_uuid"], self.text_question.uuid.hex)
        self.assertEqual(call_id["answer"], "EXT-2026-042")

    def test_select_answer_is_human_readable(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.get(self.url)

        metadata = response.data["project_metadata"]
        project_type = metadata[1]
        self.assertEqual(project_type["question_type"], QuestionTypes.SINGLE_SELECT)
        # Stored option UUID is resolved to its label.
        self.assertEqual(project_type["answer"], "Academic")

    def test_project_member_can_read_metadata(self):
        member = factories.UserFactory()
        self.project.add_user(member, ProjectRole.MEMBER)

        self.client.force_authenticate(user=member)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["project_metadata"]), 2)

    def test_metadata_is_read_only(self):
        """project_metadata is computed, not writable; PATCH ignores it."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.patch(
            self.url, {"project_metadata": [{"question": "x"}]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Still reflects the real answers, not the submitted payload.
        self.assertEqual(len(response.data["project_metadata"]), 2)

    def test_list_includes_metadata_per_project(self):
        """The list endpoint (bulk path) returns each project's own answers."""
        second = factories.ProjectFactory(customer=self.project.customer)
        second_completion, _ = (
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=self.checklist,
                scope_content_type=ContentType.objects.get_for_model(type(second)),
                scope_object_id=second.id,
            )
        )
        checklist_factories.AnswerFactory(
            completion=second_completion,
            question=self.text_question,
            answer_data="EXT-2026-099",
            user=self.fixture.owner,
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(reverse("project-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        projects = (
            response.data["results"]
            if isinstance(response.data, dict) and "results" in response.data
            else response.data
        )
        by_uuid = {p["uuid"]: p for p in projects}
        self.assertEqual(
            by_uuid[self.project.uuid.hex]["project_metadata"][0]["answer"],
            "EXT-2026-042",
        )
        self.assertEqual(
            by_uuid[second.uuid.hex]["project_metadata"][0]["answer"],
            "EXT-2026-099",
        )

    def test_empty_when_customer_has_no_metadata_checklist(self):
        other = fixtures.ProjectFixture()
        url = reverse("project-detail", kwargs={"uuid": other.project.uuid})

        self.client.force_authenticate(user=other.owner)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["project_metadata"], [])

    def test_empty_when_checklist_configured_but_unanswered(self):
        """Completion exists (auto-created) but no answers submitted -> empty, no error."""
        new_project = factories.ProjectFactory(customer=self.project.customer)
        # The completion is auto-created for the new project, but it has no answers.
        self.assertTrue(
            checklist_models.ChecklistCompletion.objects.filter(
                checklist=self.checklist,
                scope_content_type=ContentType.objects.get_for_model(type(new_project)),
                scope_object_id=new_project.id,
            ).exists()
        )

        url = reverse("project-detail", kwargs={"uuid": new_project.uuid})
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["project_metadata"], [])
