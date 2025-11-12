import base64

from ddt import ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist import models
from waldur_core.checklist.tests import factories
from waldur_core.media import models as media_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import enums


@ddt
class ProjectMetadataFileAccessTest(test.APITransactionTestCase):
    """Test file access for project metadata checklists with proper permission delegation."""

    def setUp(self):
        # Create project metadata checklist
        self.metadata_checklist = factories.ChecklistFactory(
            name="Project Metadata Checklist",
            checklist_type=enums.ChecklistTypes.PROJECT_METADATA,
        )

        self.structure_fixture = structure_fixtures.ProjectFixture()
        self.user = self.structure_fixture.user
        self.staff = self.structure_fixture.staff
        self.admin = self.structure_fixture.admin
        self.other_user = structure_factories.UserFactory()

        # Load test content
        import os

        test_dir = os.path.dirname(__file__)
        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        with open(os.path.join(test_dir, "minimal_word_file.docx"), "rb") as f:
            self.docx_content = f.read()
            self.docx_base64 = base64.b64encode(self.docx_content).decode("utf-8")

    def test_project_metadata_architecture_diagram_upload(self):
        """Test uploading and accessing project architecture diagrams."""
        # Create question for architecture diagram
        architecture_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.FILE,
            description="Upload system architecture diagram",
            required=True,
            allowed_file_types=[".pdf", ".png", ".jpg", ".svg"],
            allowed_mime_types=["application/pdf", "image/*"],
            max_file_size_mb=25,
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.metadata_checklist,
            scope=self.structure_fixture.project,
        )

        # Upload architecture diagram
        architecture_file = {
            "name": "system_architecture_v2.pdf",
            "content": self.pdf_base64,
        }
        answer = models.Answer.objects.create(
            user=self.user,
            question=architecture_question,
            completion=completion,
            answer_data=architecture_file,
        )

        stored_file_id = answer.answer_data["stored_file_id"]
        media_url = reverse("media", kwargs={"uuid": stored_file_id})

        # Project member can access project metadata
        self.client.force_authenticate(user=self.user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

        # Staff can access for administrative purposes
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Outside user cannot access project metadata
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_metadata_documentation_bundle_upload(self):
        """Test uploading multiple project documentation files."""
        # Create question for project documentation bundle
        docs_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            description="Upload complete project documentation package",
            required=True,
            max_files_count=20,
            allowed_file_types=[".pdf", ".doc", ".docx", ".md"],
            max_file_size_mb=50,
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.metadata_checklist,
            scope=self.structure_fixture.project,
        )

        # Upload comprehensive documentation bundle
        docs_bundle = [
            {"name": "api_specification.pdf", "content": self.pdf_base64},
            {"name": "deployment_guide.pdf", "content": self.pdf_base64},
            {"name": "user_manual.docx", "content": self.docx_base64},
            {"name": "technical_requirements.pdf", "content": self.pdf_base64},
        ]

        answer = models.Answer.objects.create(
            user=self.admin,  # Project admin uploads documentation
            question=docs_question,
            completion=completion,
            answer_data=docs_bundle,
        )

        # Verify documentation bundle processing
        processed_files = answer.answer_data
        self.assertEqual(len(processed_files), 4)

        expected_files = {
            "api_specification.pdf",
            "deployment_guide.pdf",
            "user_manual.docx",
            "technical_requirements.pdf",
        }
        actual_files = {f["name"] for f in processed_files}
        self.assertEqual(actual_files, expected_files)

        # Test access to all documentation files
        self.client.force_authenticate(user=self.staff)

        for file_data in processed_files:
            media_url = reverse("media", kwargs={"uuid": file_data["stored_file_id"]})
            response = self.client.get(media_url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn(file_data["name"], response["Content-Disposition"])

            # Verify content based on file type
            if file_data["name"].endswith(".pdf"):
                self.assertEqual(response.content, self.pdf_content)
                self.assertEqual(response["Content-Type"], "application/pdf")
            elif file_data["name"].endswith(".docx"):
                self.assertEqual(response.content, self.docx_content)

    def test_project_metadata_cross_project_isolation(self):
        """Test that project metadata files maintain security boundaries between projects."""
        # Create metadata question
        metadata_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.FILE,
            description="Upload project configuration file",
            required=True,
        )

        # Create two separate projects
        project1 = self.structure_fixture.project
        project2_fixture = structure_fixtures.ProjectFixture()
        project2 = project2_fixture.project

        # Create completions for both projects
        completion1 = models.ChecklistCompletion.objects.create(
            checklist=self.metadata_checklist, scope=project1
        )
        completion2 = models.ChecklistCompletion.objects.create(
            checklist=self.metadata_checklist, scope=project2
        )

        # Upload metadata files for each project
        metadata_file = {"name": "project_config.pdf", "content": self.pdf_base64}

        answer1 = models.Answer.objects.create(
            user=self.user,  # project1 user
            question=metadata_question,
            completion=completion1,
            answer_data=metadata_file,
        )

        answer2 = models.Answer.objects.create(
            user=project2_fixture.user,  # project2 user
            question=metadata_question,
            completion=completion2,
            answer_data=metadata_file,
        )

        file1_id = answer1.answer_data["stored_file_id"]
        file2_id = answer2.answer_data["stored_file_id"]

        url1 = reverse("media", kwargs={"uuid": file1_id})
        url2 = reverse("media", kwargs={"uuid": file2_id})

        # Project1 user should only access their project's metadata
        self.client.force_authenticate(user=self.user)
        response1 = self.client.get(url1)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)  # Own project

        response2 = self.client.get(url2)
        self.assertEqual(
            response2.status_code, status.HTTP_404_NOT_FOUND
        )  # Other project

        # Project2 user should only access their project's metadata
        self.client.force_authenticate(user=project2_fixture.user)
        response1 = self.client.get(url1)
        self.assertEqual(
            response1.status_code, status.HTTP_404_NOT_FOUND
        )  # Other project

        response2 = self.client.get(url2)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)  # Own project

        # Staff should access metadata from all projects (administrative access)
        self.client.force_authenticate(user=self.staff)
        response1 = self.client.get(url1)
        response2 = self.client.get(url2)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

    def test_project_metadata_team_collaboration(self):
        """Test that project team members can collaborate on metadata files."""
        # Create questions for team-uploaded metadata
        specs_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.FILE,
            description="Upload technical specifications",
            required=True,
        )

        design_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            description="Upload design documents",
            max_files_count=10,
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.metadata_checklist,
            scope=self.structure_fixture.project,
        )

        # Different team members upload different types of metadata
        specs_answer = models.Answer.objects.create(
            user=self.user,  # Developer uploads specs
            question=specs_question,
            completion=completion,
            answer_data={"name": "technical_specs.pdf", "content": self.pdf_base64},
        )

        design_files = [
            {"name": "ui_mockups.pdf", "content": self.pdf_base64},
            {"name": "database_schema.pdf", "content": self.pdf_base64},
        ]
        design_answer = models.Answer.objects.create(
            user=self.admin,  # Admin uploads design docs
            question=design_question,
            completion=completion,
            answer_data=design_files,
        )

        # All project team members should access all project metadata
        specs_url = reverse(
            "media", kwargs={"uuid": specs_answer.answer_data["stored_file_id"]}
        )

        # Original uploader can access
        self.client.force_authenticate(user=self.user)
        response = self.client.get(specs_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Other project members can access
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(specs_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Test access to design files
        for file_data in design_answer.answer_data:
            design_url = reverse("media", kwargs={"uuid": file_data["stored_file_id"]})

            # Design uploader can access
            self.client.force_authenticate(user=self.admin)
            response = self.client.get(design_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Other project members can access
            self.client.force_authenticate(user=self.user)
            response = self.client.get(design_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_project_metadata_file_validation_and_access(self):
        """Test project metadata file validation and subsequent access patterns."""
        # Create metadata question with specific validation rules
        validated_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.FILE,
            description="Upload validated project charter",
            required=True,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=5,
            # No review requirement - just validation
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.metadata_checklist,
            scope=self.structure_fixture.project,
        )

        # Upload validated metadata file
        charter_file = {"name": "project_charter_final.pdf", "content": self.pdf_base64}
        answer = models.Answer.objects.create(
            user=self.user,
            question=validated_question,
            completion=completion,
            answer_data=charter_file,
        )

        # Verify file passes validation and is stored
        self.assertFalse(answer.requires_review)  # No review required
        stored_file_id = answer.answer_data["stored_file_id"]

        # Verify file is accessible according to permission rules
        media_url = reverse("media", kwargs={"uuid": stored_file_id})

        # File uploader can access
        self.client.force_authenticate(user=self.user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

        # Project members can access metadata
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Staff can access for administrative oversight
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify file metadata
        self.assertEqual(answer.answer_data["name"], "project_charter_final.pdf")
        self.assertEqual(answer.answer_data["mime_type"], "application/pdf")
        self.assertEqual(answer.answer_data["size"], len(self.pdf_content))

        # Verify stored file
        stored_file = media_models.File.objects.get(uuid=stored_file_id)
        self.assertTrue(stored_file.name.startswith("checklist_files/"))
        self.assertIn("project_charter_final.pdf", stored_file.name)
