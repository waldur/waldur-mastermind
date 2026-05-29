import base64
import os

from django.contrib.contenttypes.models import ContentType
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import models, utils
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.media import models as media_models
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import ProjectFactory

from .. import enums


@ddt
class FileQuestionValidationTest(test.APITestCase):
    """Test file validation for FILE and MULTIPLE_FILES question types with base64 content."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.fixture = fixtures.CheckListFixture()

        # Load real test files
        test_dir = os.path.dirname(__file__)

        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        with open(os.path.join(test_dir, "minimal_word_file.docx"), "rb") as f:
            self.docx_content = f.read()
            self.docx_base64 = base64.b64encode(self.docx_content).decode("utf-8")

    def test_file_question_accepts_pdf_upload(self):
        """Test that FILE questions accept valid PDF files with base64 content."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
        )

        pdf_file_data = {"name": "research_report.pdf", "content": self.pdf_base64}

        self.assertTrue(question.is_valid_answer(pdf_file_data))

    def test_file_question_accepts_docx_upload(self):
        """Test that FILE questions accept valid DOCX files with base64 content."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".docx"],
            allowed_mime_types=[
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ],
            max_file_size_mb=10,
        )

        docx_file_data = {"name": "project_proposal.docx", "content": self.docx_base64}

        self.assertTrue(question.is_valid_answer(docx_file_data))

    def test_multiple_files_question_accepts_pdf_and_docx_uploads(self):
        """Test that MULTIPLE_FILES questions accept mixed PDF and DOCX files."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            allowed_file_types=[".pdf", ".docx"],
            allowed_mime_types=[
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ],
            max_file_size_mb=10,
            max_files_count=5,
        )

        mixed_files_data = [
            {"name": "technical_specification.pdf", "content": self.pdf_base64},
            {"name": "user_manual.docx", "content": self.docx_base64},
        ]

        self.assertTrue(question.is_valid_answer(mixed_files_data))

    def test_file_question_rejects_malicious_content(self):
        """Test that FILE questions reject files with mismatched extension and MIME type."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
        )

        # Create fake PDF (text file with .pdf extension)
        fake_pdf_content = b"This is not a PDF file"
        fake_pdf_base64 = base64.b64encode(fake_pdf_content).decode("utf-8")

        malicious_file_data = {"name": "fake_document.pdf", "content": fake_pdf_base64}

        # Should fail because actual MIME type is text/plain, not application/pdf
        self.assertFalse(question.is_valid_answer(malicious_file_data))

    def test_file_question_invalid_base64(self):
        """Test that FILE questions reject invalid base64 content."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
        )

        invalid_file_data = {
            "name": "document.pdf",
            "content": "invalid_base64_content!",
        }

        self.assertFalse(question.is_valid_answer(invalid_file_data))

    def test_file_question_missing_required_fields(self):
        """Test that FILE questions reject incomplete file data."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
        )

        # Missing 'name' field
        file_data = {"content": self.pdf_base64}
        self.assertFalse(question.is_valid_answer(file_data))

        # Missing 'content' field
        file_data = {"name": "test.pdf"}
        self.assertFalse(question.is_valid_answer(file_data))

    def test_multiple_files_question_exceeds_count_limit(self):
        """Test that MULTIPLE_FILES questions reject too many files."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            allowed_file_types=[".pdf"],
            max_files_count=2,
        )

        files_data = [
            {"name": "doc1.pdf", "content": self.pdf_base64},
            {"name": "doc2.pdf", "content": self.pdf_base64},
            {"name": "doc3.pdf", "content": self.pdf_base64},  # Exceeds limit
        ]

        self.assertFalse(question.is_valid_answer(files_data))

    def test_file_question_exceeds_size_limit(self):
        """Test that FILE questions reject oversized files."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            max_file_size_mb=1,  # Very small limit
        )

        # Create large content (2MB worth)
        large_content = b"A" * (1024 * 1024 * 2)
        large_base64 = base64.b64encode(large_content).decode("utf-8")

        large_file_data = {"name": "large_file.txt", "content": large_base64}

        self.assertFalse(question.is_valid_answer(large_file_data))

    def test_file_question_wildcard_mime_types(self):
        """Test wildcard MIME type validation."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_mime_types=["text/*"],
            max_file_size_mb=10,
        )

        # Create text file content
        text_content = b"This is a text file"
        text_base64 = base64.b64encode(text_content).decode("utf-8")

        text_file_data = {"name": "notes.txt", "content": text_base64}

        self.assertTrue(question.is_valid_answer(text_file_data))

    def test_file_question_accepts_already_processed_file(self):
        """Already-processed file (stored_file_id, no content) must pass validation."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
        )

        processed_file_data = {
            "name": "report.pdf",
            "size": 12345,
            "mime_type": "application/pdf",
            "stored_file_id": "abc123def456abc123def456abc123de",
        }

        self.assertTrue(question.is_valid_answer(processed_file_data))

    def test_multiple_files_question_accepts_mixed_processed_and_new_files(self):
        """Mixed array of already-processed and new files must pass validation."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
            max_files_count=5,
        )

        mixed_files_data = [
            # Already processed (returned from backend earlier)
            {
                "name": "existing.pdf",
                "size": 744347,
                "mime_type": "application/pdf",
                "stored_file_id": "abc123def456abc123def456abc123de",
            },
            # New upload (base64 content)
            {"name": "new_upload.pdf", "content": self.pdf_base64},
        ]

        self.assertTrue(question.is_valid_answer(mixed_files_data))

    def test_multiple_files_question_accepts_all_already_processed(self):
        """Array of all already-processed files must pass validation."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            max_files_count=5,
        )

        processed_files = [
            {
                "name": "file1.pdf",
                "size": 1000,
                "mime_type": "application/pdf",
                "stored_file_id": "aaa111bbb222aaa111bbb222aaa111bb",
            },
            {
                "name": "file2.pdf",
                "size": 2000,
                "mime_type": "application/pdf",
                "stored_file_id": "ccc333ddd444ccc333ddd444ccc333dd",
            },
        ]

        self.assertTrue(question.is_valid_answer(processed_files))


@ddt
class FileQuestionAnswerProcessingTest(test.APITestCase):
    """Test file answer processing and storage."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.checklist = factories.ChecklistFactory()

        # Create completion manually since no factory exists
        project = ProjectFactory()
        self.completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope_content_type=ContentType.objects.get_for_model(project),
            scope_object_id=project.id,
            scope=project,
        )

        # Load real test files
        test_dir = os.path.dirname(__file__)

        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

    def test_file_answer_processing_stores_content(self):
        """Test that file answers are processed and stored correctly."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
        )

        # Create answer with base64 content
        file_data = {"name": "test_document.pdf", "content": self.pdf_base64}

        answer = models.Answer.objects.create(
            user=self.fixture.staff,
            question=question,
            answer_data=file_data,
            completion=self.completion,
        )

        # Check that the answer was processed and content stored
        processed_data = answer.answer_data
        self.assertIsInstance(processed_data, dict)
        self.assertEqual(processed_data["name"], "test_document.pdf")
        self.assertEqual(processed_data["mime_type"], "application/pdf")
        self.assertIn("stored_file_id", processed_data)
        self.assertIn("size", processed_data)

        # Verify the file was stored in media system
        stored_file = media_models.File.objects.get(
            uuid=processed_data["stored_file_id"]
        )
        self.assertEqual(stored_file.content, self.pdf_content)
        self.assertEqual(stored_file.mime_type, "application/pdf")

    def test_multiple_files_answer_processing(self):
        """Test that multiple file answers are processed correctly."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            max_files_count=3,
        )

        # Create text file content for testing
        text_content = b"This is a test file"
        text_base64 = base64.b64encode(text_content).decode("utf-8")

        files_data = [
            {"name": "document1.pdf", "content": self.pdf_base64},
            {"name": "notes.txt", "content": text_base64},
        ]

        answer = models.Answer.objects.create(
            user=self.fixture.staff,
            question=question,
            answer_data=files_data,
            completion=self.completion,
        )

        # Check that all files were processed
        processed_data = answer.answer_data
        self.assertIsInstance(processed_data, list)
        self.assertEqual(len(processed_data), 2)

        # Check first file (PDF)
        pdf_file = processed_data[0]
        self.assertEqual(pdf_file["name"], "document1.pdf")
        self.assertEqual(pdf_file["mime_type"], "application/pdf")
        self.assertIn("stored_file_id", pdf_file)

        # Check second file (text)
        text_file = processed_data[1]
        self.assertEqual(text_file["name"], "notes.txt")
        self.assertEqual(text_file["mime_type"], "text/plain")
        self.assertIn("stored_file_id", text_file)


@ddt
class FileQuestionAdminAPITest(test.APITestCase):
    """Test admin API for creating and managing file questions."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = factories.QuestionFactory.get_admin_list_url()
        self.checklist = factories.ChecklistFactory()

    def _get_file_question_payload(self):
        return {
            "description": "Upload your document",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.FILE,
            "allowed_file_types": [".pdf", ".doc", ".docx"],
            "allowed_mime_types": [
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ],
            "max_file_size_mb": 10,
        }

    def _get_multiple_files_question_payload(self):
        return {
            "description": "Upload supporting documents",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.MULTIPLE_FILES,
            "allowed_file_types": [".pdf", ".png", ".jpg"],
            "allowed_mime_types": ["application/pdf", "image/png", "image/jpeg"],
            "max_file_size_mb": 5,
            "max_files_count": 3,
        }

    @data("staff")
    def test_user_can_create_file_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = self._get_file_question_payload()
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(uuid=response.data["uuid"])
        self.assertEqual(question.question_type, enums.QuestionTypes.FILE)
        self.assertEqual(question.allowed_file_types, [".pdf", ".doc", ".docx"])
        self.assertEqual(question.max_file_size_mb, 10)

    @data("staff")
    def test_user_can_create_multiple_files_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = self._get_multiple_files_question_payload()
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = models.Question.objects.get(uuid=response.data["uuid"])
        self.assertEqual(question.question_type, enums.QuestionTypes.MULTIPLE_FILES)
        self.assertEqual(question.max_files_count, 3)

    @data("staff")
    def test_file_validation_fields_rejected_for_non_file_questions(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "description": "Text question",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "allowed_file_types": [".pdf"],  # Should be rejected
            "max_file_size_mb": 10,  # Should be rejected
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("File validation fields", str(response.data))

    @data("staff")
    def test_invalid_file_types_format_rejected(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = self._get_file_question_payload()
        payload["allowed_file_types"] = ["pdf", "doc"]  # Missing dots

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid file type", str(response.data))

    @data("staff")
    def test_invalid_mime_type_format_rejected(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = self._get_file_question_payload()
        payload["allowed_mime_types"] = [
            "invalid-mime-type",
            "application/pdf",
        ]  # Invalid format

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("MIME types must be in format", str(response.data))


class FileQuestionIntegrationTest(test.APITestCase):
    """Test end-to-end file upload through answer submission API."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.checklist = factories.ChecklistFactory()

        # Load real test files
        test_dir = os.path.dirname(__file__)

        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

    def test_file_upload_via_answer_submission(self):
        """Test complete file upload workflow through checklist answer submission."""
        # This test would require the actual checklist integration endpoints
        # For now, just test the core validation and processing

        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
        )

        # Create completion manually since no factory exists
        project = ProjectFactory()
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope_content_type=ContentType.objects.get_for_model(project),
            scope_object_id=project.id,
            scope=project,
        )

        # Submit file answer
        file_data = {"name": "compliance_document.pdf", "content": self.pdf_base64}

        answer = models.Answer.objects.create(
            user=self.fixture.staff,
            question=question,
            answer_data=file_data,
            completion=completion,
        )

        # Verify the answer was processed correctly
        self.assertIsNotNone(answer.answer_data)
        processed_data = answer.answer_data

        # Should have metadata, not raw base64
        self.assertNotIn("content", processed_data)
        self.assertIn("stored_file_id", processed_data)
        self.assertEqual(processed_data["mime_type"], "application/pdf")
        self.assertEqual(processed_data["name"], "compliance_document.pdf")


class FileAnswerUpdateTest(test.APITestCase):
    """Test that updating file answers processes the new file content correctly."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.checklist = factories.ChecklistFactory()

        test_dir = os.path.dirname(__file__)

        with open(os.path.join(test_dir, "minimal_word_file.pdf"), "rb") as f:
            self.pdf_content = f.read()
            self.pdf_base64 = base64.b64encode(self.pdf_content).decode("utf-8")

        project = ProjectFactory()
        self.completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist,
            scope_content_type=ContentType.objects.get_for_model(project),
            scope_object_id=project.id,
            scope=project,
        )

    def test_update_file_answer_processes_new_file(self):
        """Test that updating a file answer replaces old file with new processed file."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
        )

        # Create initial answer
        initial_data = {"name": "original.pdf", "content": self.pdf_base64}
        answer, created = models.Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=self.fixture.staff,
            defaults={"answer_data": initial_data},
        )
        self.assertTrue(created)
        initial_stored_id = answer.answer_data.get("stored_file_id")
        self.assertIsNotNone(initial_stored_id)

        # Update with new file via update_or_create
        new_data = {"name": "updated.pdf", "content": self.pdf_base64}
        updated_answer, created = models.Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=self.fixture.staff,
            defaults={"answer_data": new_data},
        )
        self.assertFalse(created)

        updated_answer.refresh_from_db()
        processed = updated_answer.answer_data

        # Must be processed, not raw
        self.assertIsInstance(processed, dict)
        self.assertNotIn("content", processed)
        self.assertIn("stored_file_id", processed)
        self.assertEqual(processed["name"], "updated.pdf")

    def test_update_multiple_files_answer_processes_new_files(self):
        """Test that updating a multiple_files answer processes all new files."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
        )

        text_content = b"First file content"
        text_base64 = base64.b64encode(text_content).decode("utf-8")

        # Create initial answer with one file
        initial_data = [{"name": "first.pdf", "content": self.pdf_base64}]
        models.Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=self.fixture.staff,
            defaults={"answer_data": initial_data},
        )

        # Update with two files
        new_data = [
            {"name": "new_doc.pdf", "content": self.pdf_base64},
            {"name": "notes.txt", "content": text_base64},
        ]
        updated_answer, created = models.Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=self.fixture.staff,
            defaults={"answer_data": new_data},
        )
        self.assertFalse(created)

        updated_answer.refresh_from_db()
        processed = updated_answer.answer_data

        # Must be list of processed files, not raw
        self.assertIsInstance(processed, list)
        self.assertEqual(len(processed), 2)
        for f in processed:
            self.assertNotIn("content", f)
            self.assertIn("stored_file_id", f)

    def test_idempotent_update_does_not_duplicate_files(self):
        """Test that re-submitting already-processed file data does not create duplicate files."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
        )

        # Create initial answer (processes the file)
        initial_data = {"name": "doc.pdf", "content": self.pdf_base64}
        answer, _ = models.Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=self.fixture.staff,
            defaults={"answer_data": initial_data},
        )
        answer.refresh_from_db()
        stored_id_before = answer.answer_data["stored_file_id"]
        file_count_before = media_models.File.objects.count()

        # Re-submit already-processed data (simulates frontend sending back server response)
        processed_data = answer.answer_data  # Already has stored_file_id, no content
        models.Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=self.fixture.staff,
            defaults={"answer_data": processed_data},
        )
        answer.refresh_from_db()

        # stored_file_id must not change, no duplicate file created
        self.assertEqual(answer.answer_data["stored_file_id"], stored_id_before)
        self.assertEqual(media_models.File.objects.count(), file_count_before)


class FileQuestionUtilsTest(test.APITestCase):
    """Test utility functions for file question types."""

    def test_file_type_is_valid_answer_format(self):
        """Test is_valid_answer utility for file types."""
        # FILE type should accept dict
        self.assertTrue(utils.is_valid_answer({}, enums.QuestionTypes.FILE))
        self.assertFalse(utils.is_valid_answer([], enums.QuestionTypes.FILE))

        # MULTIPLE_FILES type should accept list
        self.assertTrue(utils.is_valid_answer([], enums.QuestionTypes.MULTIPLE_FILES))
        self.assertFalse(utils.is_valid_answer({}, enums.QuestionTypes.MULTIPLE_FILES))

    def test_file_operators_supported(self):
        """Test that appropriate operators are supported for file types."""
        # FILE type supports equals, not_equals, contains
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.FILE, "equals"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.FILE, "contains"
            )
        )

        # MULTIPLE_FILES type supports contains, in, not_in
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.MULTIPLE_FILES, "contains"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.MULTIPLE_FILES, "in"
            )
        )
