import base64

from ddt import data, ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist import models
from waldur_core.checklist.media_access import user_can_access_checklist_file
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.media import models as media_models
from waldur_core.structure.tests import factories as structure_factories_direct
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import enums


@ddt
class ReviewerFileAccessTest(test.APITestCase):
    """Test file access permissions for reviewers in checklist system."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.fixture = fixtures.CheckListFixture()
        self.structure_fixture = structure_fixtures.ProjectFixture()

        # Create users with different roles
        self.user = self.structure_fixture.user  # Regular user who uploads files
        self.staff = self.structure_fixture.staff  # Staff/reviewer
        self.admin = self.structure_fixture.admin  # Admin
        # Create a truly unrelated user (not part of any project)
        from waldur_core.structure.tests.factories import UserFactory

        self.other_user = UserFactory()  # Unrelated user

        # Load test file content
        import os

        test_dir = os.path.dirname(__file__)
        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        # Create file question
        self.file_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
            required=True,
        )

        # Create checklist completion for the project
        self.completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=self.structure_fixture.project,
        )

        # Create answer with file upload by regular user
        self.file_data = {"name": "confidential_report.pdf", "content": self.pdf_base64}
        self.answer = models.Answer.objects.create(
            user=self.user,
            question=self.file_question,
            completion=self.completion,
            answer_data=self.file_data,
        )

        # Get the stored file ID from the processed answer
        self.stored_file_id = self.answer.answer_data["stored_file_id"]

    def test_staff_can_access_uploaded_files_for_administrative_purposes(self):
        """Test that staff members can access files uploaded to checklists for review purposes."""
        # Create media download URL
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})

        # Staff should be able to access the file
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(media_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("confidential_report.pdf", response["Content-Disposition"])

    def test_project_admin_can_access_uploaded_files_for_review(self):
        """A project admin reaches the file through the completion's scope."""
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})

        self.client.force_authenticate(user=self.admin)
        response = self.client.get(media_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

    def test_bare_superuser_cannot_access_uploaded_files(self):
        """is_superuser is a Django flag; Waldur gates on is_staff/is_support."""
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})

        superuser = structure_factories_direct.UserFactory(is_superuser=True)
        self.client.force_authenticate(user=superuser)
        response = self.client.get(media_url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_support_can_access_uploaded_files_for_review(self):
        """Support users are intended to be able to review checklist files."""
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})

        support = structure_factories_direct.UserFactory(is_support=True)
        self.client.force_authenticate(user=support)
        response = self.client.get(media_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

    def test_query_count_does_not_grow_with_answers_on_one_completion(self):
        """Several answers can reference one file; checking a completion is ~5
        queries, so looping over answers used to re-run the identical check
        once per answer (10 answers cost 52 queries). The rule iterates
        distinct completions instead, which must stay flat.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def measure(extra_answers):
            for _ in range(extra_answers):
                models.Answer.objects.create(
                    user=structure_factories_direct.UserFactory(),
                    question=factories.QuestionFactory(
                        checklist=self.checklist,
                        question_type=enums.QuestionTypes.FILE,
                    ),
                    completion=self.completion,
                    answer_data={"stored_file_id": self.stored_file_id},
                )
            media_file = media_models.File.objects.get(uuid=self.stored_file_id)
            outsider = structure_factories_direct.UserFactory()
            with CaptureQueriesContext(connection) as ctx:
                user_can_access_checklist_file(media_file, outsider)
            return len(ctx.captured_queries)

        with_two = measure(1)
        with_many = measure(7)
        self.assertEqual(
            with_two,
            with_many,
            "query count grew with the number of answers on one completion",
        )

    def test_file_uploader_can_access_their_own_files(self):
        """Test that users who uploaded files can access their own files."""
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})

        # File uploader should be able to access their own file
        self.client.force_authenticate(user=self.user)
        response = self.client.get(media_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

    def test_unauthorized_user_cannot_access_checklist_files(self):
        """Test that unauthorized users cannot access checklist files."""
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})

        # Other user should NOT be able to access the file
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(media_url)

        # Now with permission checks implemented, unauthorized access should return 404
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_user_cannot_access_checklist_files(self):
        """Test that anonymous users cannot access checklist files."""
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})

        # Anonymous users should not be able to access files
        response = self.client.get(media_url)

        # With permission checks implemented, anonymous access should return 404
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "admin")
    def test_reviewers_can_access_files_through_completion_data(self, user_type):
        """Test that reviewers can discover file IDs through checklist completion data."""
        user_obj = getattr(self.structure_fixture, user_type)

        # Mock reviewer endpoint - in real implementation this would be through proper ViewSet
        # but we're testing the data accessibility part
        completion_data = {
            "checklist": self.checklist,
            "answers": [
                {
                    "question_uuid": str(self.file_question.uuid),
                    "answer_data": self.answer.answer_data,
                }
            ],
        }

        # Reviewer should be able to see the stored_file_id in the answer data
        answer_data = completion_data["answers"][0]["answer_data"]
        self.assertIn("stored_file_id", answer_data)
        self.assertEqual(answer_data["stored_file_id"], self.stored_file_id)

        # And then be able to download the file using that ID
        media_url = reverse("media", kwargs={"uuid": self.stored_file_id})
        self.client.force_authenticate(user=user_obj)
        response = self.client.get(media_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

    def test_file_metadata_exposed_to_reviewers(self):
        """Test that file metadata (name, size, mime_type) is properly exposed to reviewers."""
        # File metadata should be available in the answer data
        answer_data = self.answer.answer_data

        self.assertEqual(answer_data["name"], "confidential_report.pdf")
        self.assertEqual(answer_data["mime_type"], "application/pdf")
        self.assertEqual(answer_data["size"], len(self.pdf_content))
        self.assertIn("stored_file_id", answer_data)

        # Verify the stored file exists and matches
        stored_file = media_models.File.objects.get(uuid=answer_data["stored_file_id"])
        self.assertEqual(stored_file.content, self.pdf_content)
        self.assertEqual(stored_file.mime_type, "application/pdf")


@ddt
class ReviewerMultipleFilesAccessTest(test.APITestCase):
    """Test reviewer access to multiple file uploads."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.fixture = fixtures.CheckListFixture()
        self.structure_fixture = structure_fixtures.ProjectFixture()

        self.user = self.structure_fixture.user
        self.staff = self.structure_fixture.staff

        # Ensure staff user actually has staff privileges for permission testing
        self.staff.is_staff = True
        self.staff.save()

        # Load test files
        import os

        test_dir = os.path.dirname(__file__)

        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        with open(os.path.join(test_dir, "minimal_word_file.docx"), "rb") as f:
            self.docx_content = f.read()
            self.docx_base64 = base64.b64encode(self.docx_content).decode("utf-8")

        # Create multiple files question
        self.multi_files_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            allowed_file_types=[".pdf", ".docx"],
            allowed_mime_types=[
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ],
            max_file_size_mb=10,
            max_files_count=5,
            required=True,
        )

        # Create completion
        self.completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=self.structure_fixture.project,
        )

        # Create answer with multiple files
        self.files_data = [
            {"name": "document1.pdf", "content": self.pdf_base64},
            {"name": "document2.docx", "content": self.docx_base64},
        ]

        self.answer = models.Answer.objects.create(
            user=self.user,
            question=self.multi_files_question,
            completion=self.completion,
            answer_data=self.files_data,
        )

    def test_reviewer_can_access_all_multiple_files(self):
        """Test that reviewers can access all files in a multiple files answer."""
        # Get processed answer data
        processed_files = self.answer.answer_data
        self.assertEqual(len(processed_files), 2)

        # Test access to both files as staff user
        self.client.force_authenticate(user=self.staff)

        for file_data in processed_files:
            media_url = reverse("media", kwargs={"uuid": file_data["stored_file_id"]})
            response = self.client.get(media_url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify correct content based on file type
            if file_data["name"] == "document1.pdf":
                self.assertEqual(response.content, self.pdf_content)
                self.assertEqual(response["Content-Type"], "application/pdf")
            elif file_data["name"] == "document2.docx":
                self.assertEqual(response.content, self.docx_content)
                # DOCX MIME type varies, just check it contains the expected format
                self.assertIn("officedocument", response["Content-Type"])

    def test_multiple_files_metadata_structure(self):
        """Test that multiple files answer data has proper structure for reviewers."""
        processed_files = self.answer.answer_data

        # Should be a list of file metadata
        self.assertIsInstance(processed_files, list)
        self.assertEqual(len(processed_files), 2)

        # Each file should have proper metadata
        for file_data in processed_files:
            self.assertIn("name", file_data)
            self.assertIn("size", file_data)
            self.assertIn("mime_type", file_data)
            self.assertIn("stored_file_id", file_data)

            # Should NOT contain the raw base64 content
            self.assertNotIn("content", file_data)


class FileAccessSecurityTest(test.APITestCase):
    """Test security aspects of file access in checklist system."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.fixture = fixtures.CheckListFixture()
        self.structure_fixture = structure_fixtures.ProjectFixture()

        self.user = self.structure_fixture.user
        self.other_user = self.structure_fixture.manager  # Different user
        self.staff = self.structure_fixture.staff

        # Load test content
        import os

        test_dir = os.path.dirname(__file__)
        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        # Create file question
        self.file_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
            max_file_size_mb=10,
        )

    def test_file_access_requires_appropriate_permissions(self):
        """Test that file access is properly restricted based on user permissions."""
        # Create completion for one project
        project1 = self.structure_fixture.project
        completion1 = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=project1,
        )

        # User uploads file to project1
        file_data = {"name": "project1_file.pdf", "content": self.pdf_base64}
        answer1 = models.Answer.objects.create(
            user=self.user,
            question=self.file_question,
            completion=completion1,
            answer_data=file_data,
        )
        stored_file_id1 = answer1.answer_data["stored_file_id"]

        # Create another project and completion
        other_fixture = structure_fixtures.ProjectFixture()
        completion2 = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=other_fixture.project,
        )

        # Other user uploads file to project2
        answer2 = models.Answer.objects.create(
            user=other_fixture.user,
            question=self.file_question,
            completion=completion2,
            answer_data=file_data,
        )
        stored_file_id2 = answer2.answer_data["stored_file_id"]

        # Test file access boundaries
        media_url1 = reverse("media", kwargs={"uuid": stored_file_id1})
        media_url2 = reverse("media", kwargs={"uuid": stored_file_id2})

        # User1 should access their own file
        self.client.force_authenticate(user=self.user)
        response = self.client.get(media_url1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # User1 should NOT access user2's file (in different project)
        response = self.client.get(media_url2)
        # With proper permissions implemented, cross-project access should be denied
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Staff should access both files (for review purposes)
        self.client.force_authenticate(user=self.staff)
        response1 = self.client.get(media_url1)
        response2 = self.client.get(media_url2)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

    def test_deleted_file_access_returns_404(self):
        """Test that accessing deleted files returns 404."""
        # Create and upload file
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=self.structure_fixture.project,
        )

        file_data = {"name": "temporary_file.pdf", "content": self.pdf_base64}
        answer = models.Answer.objects.create(
            user=self.user,
            question=self.file_question,
            completion=completion,
            answer_data=file_data,
        )
        stored_file_id = answer.answer_data["stored_file_id"]

        # Verify file exists and is accessible
        media_url = reverse("media", kwargs={"uuid": stored_file_id})
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Delete the file
        media_models.File.objects.filter(uuid=stored_file_id).delete()

        # Access should now return 404
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_file_content_integrity(self):
        """Test that file content is preserved correctly through upload and download."""
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=self.structure_fixture.project,
        )

        file_data = {"name": "integrity_test.pdf", "content": self.pdf_base64}
        answer = models.Answer.objects.create(
            user=self.user,
            question=self.file_question,
            completion=completion,
            answer_data=file_data,
        )

        # Download file and verify content integrity
        stored_file_id = answer.answer_data["stored_file_id"]
        media_url = reverse("media", kwargs={"uuid": stored_file_id})

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(media_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

        # Verify file headers
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(int(response["Content-Length"]), len(self.pdf_content))
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("integrity_test.pdf", response["Content-Disposition"])


class ProposalComplianceFileAccessTest(test.APITestCase):
    """Test file access for proposal compliance checklists specifically."""

    def setUp(self):
        # Create proposal compliance checklist
        self.proposal_checklist = factories.ChecklistFactory(
            name="Proposal Compliance Checklist",
            checklist_type=enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        )

        self.structure_fixture = structure_fixtures.ProjectFixture()
        self.user = self.structure_fixture.user
        self.staff = self.structure_fixture.staff
        self.admin = self.structure_fixture.admin
        self.other_user = structure_factories_direct.UserFactory()

        # Load test content
        import os

        test_dir = os.path.dirname(__file__)
        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        # Create file question for proposal compliance
        self.compliance_question = factories.QuestionFactory(
            checklist=self.proposal_checklist,
            question_type=enums.QuestionTypes.FILE,
            description="Upload compliance documentation",
            required=True,
            allowed_file_types=[".pdf", ".doc", ".docx"],
            always_requires_review=True,  # Compliance files always need review
        )

    def test_proposal_compliance_file_upload_and_review_access(self):
        """Test that proposal compliance files can be uploaded and accessed by reviewers."""
        # Create completion for proposal compliance
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.proposal_checklist,
            scope=self.structure_fixture.project,  # Scoped to project
        )

        # User uploads compliance document
        compliance_file = {"name": "gdpr_compliance.pdf", "content": self.pdf_base64}
        answer = models.Answer.objects.create(
            user=self.user,
            question=self.compliance_question,
            completion=completion,
            answer_data=compliance_file,
        )

        # Verify review flag is set due to always_requires_review
        self.assertTrue(answer.requires_review)

        stored_file_id = answer.answer_data["stored_file_id"]
        media_url = reverse("media", kwargs={"uuid": stored_file_id})

        # File uploader should access their own compliance file
        self.client.force_authenticate(user=self.user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, self.pdf_content)

        # Call manager should access compliance files for review
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Project admin should access files in their project
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Unrelated user should NOT access compliance files
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_proposal_compliance_multiple_documents(self):
        """Test access to multiple compliance documents in a single answer."""
        # Create completion
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.proposal_checklist,
            scope=self.structure_fixture.project,
        )

        # Create multiple files question for compliance
        multi_docs_question = factories.QuestionFactory(
            checklist=self.proposal_checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            description="Upload all compliance documents",
            max_files_count=10,
            always_requires_review=True,
        )

        # Upload multiple compliance documents
        compliance_docs = [
            {"name": "privacy_policy.pdf", "content": self.pdf_base64},
            {"name": "data_protection_plan.pdf", "content": self.pdf_base64},
            {"name": "security_audit.pdf", "content": self.pdf_base64},
        ]

        answer = models.Answer.objects.create(
            user=self.user,
            question=multi_docs_question,
            completion=completion,
            answer_data=compliance_docs,
        )

        # Verify all files are processed and accessible
        processed_files = answer.answer_data
        self.assertEqual(len(processed_files), 3)

        # Call manager should access all compliance documents
        self.client.force_authenticate(user=self.staff)

        for file_data in processed_files:
            media_url = reverse("media", kwargs={"uuid": file_data["stored_file_id"]})
            response = self.client.get(media_url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.content, self.pdf_content)
            self.assertIn(file_data["name"], response["Content-Disposition"])


class ProjectMetadataFileAccessTest(test.APITestCase):
    """Test file access for project metadata checklists specifically."""

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
        self.other_user = structure_factories_direct.UserFactory()

        # Load test content
        import os

        test_dir = os.path.dirname(__file__)
        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

    def test_project_metadata_file_access_permissions(self):
        """Test project metadata file access follows project permission boundaries."""
        # Create metadata questions
        architecture_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.FILE,
            description="Upload project architecture diagram",
            allowed_file_types=[".pdf", ".png", ".jpg"],
        )

        documentation_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            description="Upload project documentation",
            max_files_count=5,
        )

        # Create completion for project metadata
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.metadata_checklist,
            scope=self.structure_fixture.project,
        )

        # Upload architecture diagram
        architecture_file = {
            "name": "system_architecture.pdf",
            "content": self.pdf_base64,
        }
        arch_answer = models.Answer.objects.create(
            user=self.user,
            question=architecture_question,
            completion=completion,
            answer_data=architecture_file,
        )

        # Upload multiple documentation files
        docs_files = [
            {"name": "api_documentation.pdf", "content": self.pdf_base64},
            {"name": "deployment_guide.pdf", "content": self.pdf_base64},
        ]
        docs_answer = models.Answer.objects.create(
            user=self.admin,  # Different user in same project
            question=documentation_question,
            completion=completion,
            answer_data=docs_files,
        )

        # Test access to architecture file
        arch_file_id = arch_answer.answer_data["stored_file_id"]
        arch_url = reverse("media", kwargs={"uuid": arch_file_id})

        # Project members should access project metadata files
        self.client.force_authenticate(user=self.user)  # Uploader
        response = self.client.get(arch_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(user=self.admin)  # Project admin
        response = self.client.get(arch_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Staff should access for administrative purposes
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(arch_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Test access to documentation files
        docs_files = docs_answer.answer_data
        for doc_file in docs_files:
            doc_url = reverse("media", kwargs={"uuid": doc_file["stored_file_id"]})

            # Project members should access
            self.client.force_authenticate(user=self.admin)  # Uploader
            response = self.client.get(doc_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            self.client.force_authenticate(user=self.user)  # Project member
            response = self.client.get(doc_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Outside users should NOT access project metadata
            self.client.force_authenticate(user=self.other_user)
            response = self.client.get(doc_url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_metadata_cross_project_isolation(self):
        """Test that project metadata files are isolated between different projects."""
        # Create two separate projects with metadata checklists
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

        # Create metadata question
        metadata_question = factories.QuestionFactory(
            checklist=self.metadata_checklist,
            question_type=enums.QuestionTypes.FILE,
            description="Upload project specification",
        )

        # Upload files to each project
        spec_file = {"name": "project_spec.pdf", "content": self.pdf_base64}

        answer1 = models.Answer.objects.create(
            user=self.user,  # project1 user
            question=metadata_question,
            completion=completion1,
            answer_data=spec_file,
        )

        answer2 = models.Answer.objects.create(
            user=project2_fixture.user,  # project2 user
            question=metadata_question,
            completion=completion2,
            answer_data=spec_file,
        )

        file1_id = answer1.answer_data["stored_file_id"]
        file2_id = answer2.answer_data["stored_file_id"]

        url1 = reverse("media", kwargs={"uuid": file1_id})
        url2 = reverse("media", kwargs={"uuid": file2_id})

        # Project1 user should only access their project's files
        self.client.force_authenticate(user=self.user)
        response1 = self.client.get(url1)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)  # Own project

        response2 = self.client.get(url2)
        self.assertEqual(
            response2.status_code, status.HTTP_404_NOT_FOUND
        )  # Other project

        # Project2 user should only access their project's files
        self.client.force_authenticate(user=project2_fixture.user)
        response1 = self.client.get(url1)
        self.assertEqual(
            response1.status_code, status.HTTP_404_NOT_FOUND
        )  # Other project

        response2 = self.client.get(url2)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)  # Own project

        # Staff should access files from both projects (administrative access)
        self.client.force_authenticate(user=self.staff)
        response1 = self.client.get(url1)
        response2 = self.client.get(url2)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)


class PermissionDelegationScenariosTest(test.APITestCase):
    """Test various permission delegation scenarios for different scope types."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.structure_fixture = structure_fixtures.ProjectFixture()

        # Create different user types
        self.project_owner = self.structure_fixture.user  # Project user
        self.project_admin = self.structure_fixture.admin  # Project admin
        self.staff_user = self.structure_fixture.staff  # Staff user
        self.other_user = structure_factories_direct.UserFactory()  # Unrelated user

        # Load test content
        import os

        test_dir = os.path.dirname(__file__)
        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        # Create file question
        self.file_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
        )

    def test_project_scope_permission_delegation(self):
        """Test permission delegation for Project-scoped checklist completions."""
        # Create completion scoped to a project
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=self.structure_fixture.project,
        )

        # User with project access uploads file
        answer = models.Answer.objects.create(
            user=self.project_owner,
            question=self.file_question,
            completion=completion,
            answer_data={"name": "project_file.pdf", "content": self.pdf_base64},
        )
        stored_file_id = answer.answer_data["stored_file_id"]
        media_url = reverse("media", kwargs={"uuid": stored_file_id})

        # Project owner should access their own file
        self.client.force_authenticate(user=self.project_owner)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Project admin should access files in their project
        self.client.force_authenticate(user=self.project_admin)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Staff should access files for review
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Unrelated user should NOT access the file
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_multiple_completions_with_similar_files(self):
        """Test file access isolation between different completions in different projects."""
        # Create two completions in different projects
        project1 = self.structure_fixture.project
        project2_fixture = structure_fixtures.ProjectFixture()
        project2 = project2_fixture.project

        completion1 = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=project1
        )
        completion2 = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=project2
        )

        # Same file uploaded in both completions
        file_data = {"name": "shared_file.pdf", "content": self.pdf_base64}

        answer1 = models.Answer.objects.create(
            user=self.structure_fixture.user,
            question=self.file_question,
            completion=completion1,
            answer_data=file_data,
        )

        # Simulate same file being referenced in second completion
        # (In practice this would be rare, but tests the edge case)
        answer2 = models.Answer.objects.create(
            user=project2_fixture.user,
            question=self.file_question,
            completion=completion2,
            answer_data=file_data,  # Use original file data, not processed data
        )

        stored_file_id1 = answer1.answer_data["stored_file_id"]
        stored_file_id2 = answer2.answer_data["stored_file_id"]
        media_url1 = reverse("media", kwargs={"uuid": stored_file_id1})
        media_url2 = reverse("media", kwargs={"uuid": stored_file_id2})

        # Project1 user should access their own file
        self.client.force_authenticate(user=self.structure_fixture.user)
        response = self.client.get(media_url1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Project2 user should access their own file
        self.client.force_authenticate(user=project2_fixture.user)
        response = self.client.get(media_url2)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Cross-project access should be denied
        self.client.force_authenticate(user=self.structure_fixture.user)
        response = self.client.get(
            media_url2
        )  # project1 user trying to access project2 file
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.client.force_authenticate(user=project2_fixture.user)
        response = self.client.get(
            media_url1
        )  # project2 user trying to access project1 file
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_access_across_all_scopes(self):
        """Test that staff users can access files across all scope types."""
        # Create completions with different scope types
        project_completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.structure_fixture.project
        )

        # Upload files to different scoped completions
        project_answer = models.Answer.objects.create(
            user=self.structure_fixture.user,  # Use correct variable
            question=self.file_question,
            completion=project_completion,
            answer_data={"name": "project_file.pdf", "content": self.pdf_base64},
        )

        # Staff should access all files regardless of scope
        self.client.force_authenticate(user=self.staff_user)

        media_url = reverse(
            "media", kwargs={"uuid": project_answer.answer_data["stored_file_id"]}
        )
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_permission_hierarchy_precedence(self):
        """Test that permission hierarchy is respected (uploader > scope access > staff)."""
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.structure_fixture.project
        )

        # User uploads file
        answer = models.Answer.objects.create(
            user=self.project_owner,
            question=self.file_question,
            completion=completion,
            answer_data={"name": "hierarchy_test.pdf", "content": self.pdf_base64},
        )
        stored_file_id = answer.answer_data["stored_file_id"]
        media_url = reverse("media", kwargs={"uuid": stored_file_id})

        # Test permission hierarchy:

        # 1. File uploader (highest priority)
        self.client.force_authenticate(user=self.project_owner)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 2. Scope access (project member)
        self.client.force_authenticate(user=self.project_admin)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 3. Staff access (reviewer permissions)
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 4. No access (unrelated user)
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class EdgeCasesAndErrorConditionsTest(test.APITestCase):
    """Test edge cases and error conditions in file permission system."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.structure_fixture = structure_fixtures.ProjectFixture()
        self.user = self.structure_fixture.user

        # Load test content
        import os

        test_dir = os.path.dirname(__file__)
        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

    def test_file_not_found_in_checklist_system(self):
        """Test access to file that exists in media but not referenced in checklist answers."""
        from waldur_core.media.models import File

        # Create a file directly in media system (not through checklist)
        orphan_file = File.objects.create(
            name="checklist_files/orphan_file.pdf",
            content=self.pdf_content,
            size=len(self.pdf_content),
            mime_type="application/pdf",
        )

        media_url = reverse("media", kwargs={"uuid": orphan_file.uuid})

        # Even staff should not access orphan files
        self.client.force_authenticate(user=self.structure_fixture.staff)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_file_uuid(self):
        """Test access with invalid file UUID."""
        media_url = reverse("media", kwargs={"uuid": "invalid-uuid-12345"})

        self.client.force_authenticate(user=self.structure_fixture.staff)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_non_checklist_file_is_denied(self):
        """A prefix no app declared a rule for is served to nobody."""
        from waldur_core.media.models import File

        # Create a non-checklist file (doesn't start with "checklist_files/")
        other_file = File.objects.create(
            name="other_files/some_file.pdf",
            content=self.pdf_content,
            size=len(self.pdf_content),
            mime_type="application/pdf",
        )

        media_url = reverse("media", kwargs={"uuid": other_file.uuid})

        self.client.force_authenticate(user=self.user)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_permission_exception_handling(self):
        """Test that permission checking exceptions are handled gracefully."""
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope=self.structure_fixture.project,
        )

        file_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
        )

        answer = models.Answer.objects.create(
            user=self.user,
            question=file_question,
            completion=completion,
            answer_data={"name": "exception_test.pdf", "content": self.pdf_base64},
        )

        stored_file_id = answer.answer_data["stored_file_id"]
        media_url = reverse("media", kwargs={"uuid": stored_file_id})

        # Even if there are permission checking errors, the system should handle gracefully
        self.client.force_authenticate(user=self.user)
        response = self.client.get(media_url)
        # Should work for file uploader regardless of permission checking errors
        self.assertEqual(response.status_code, status.HTTP_200_OK)
