from ddt import data, ddt
from rest_framework import status, test
from rest_framework.test import APIRequestFactory

from waldur_core.checklist import models, serializers
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import enums


@ddt
class QuestionAdminGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.QuestionFactory.get_admin_list_url()

    @data("staff")
    def test_user_can_list_questions(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("owner")
    def test_user_cannot_list_questions(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class QuestionAdminCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = factories.QuestionFactory.get_admin_list_url()
        self.checklist = factories.ChecklistFactory()

    def _get_payload(self):
        return {
            "description": "test question",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.TEXT_INPUT,
        }

    @data("staff")
    def test_user_can_create_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Question.objects.filter(description="test question").exists()
        )

    @data("owner")
    def test_user_cannot_create_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_operator_and_review_answer_value_are_required(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[2][0]
        payload["review_answer_value"] = ["answer"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[2][0]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Both", response.json()["non_field_errors"][0])

    def test_validate_operator(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[3][0]
        payload["review_answer_value"] = ["answer"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Operator", response.json()["non_field_errors"][0])

    def test_validate_review_answer_value(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[2][0]
        payload["review_answer_value"] = "answer"
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Review answer value", response.json()["non_field_errors"][0])


@ddt
class QuestionAdminUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = factories.QuestionFactory()
        self.url = factories.QuestionFactory.get_admin_url(self.question)

    def _get_payload(self):
        return {
            "description": "updated question",
        }

    @data("staff")
    def test_user_can_update_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.Question.objects.filter(description="updated question").exists()
        )

    @data("owner")
    def test_user_cannot_update_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.Question.objects.filter(description="updated question").exists()
        )


@ddt
class QuestionAdminDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = factories.QuestionFactory()
        self.url = factories.QuestionFactory.get_admin_url(self.question)

    @data("staff")
    def test_user_can_delete_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Question.objects.filter(pk=self.question.pk).exists())

    @data("owner")
    def test_user_cannot_delete_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Question.objects.filter(pk=self.question.pk).exists())


class QuestionAdminSerializerFieldTest(test.APITestCase):
    """Test that QuestionAdminSerializer can access all required fields."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()

    def test_serializer_has_all_required_fields(self):
        """Test that QuestionAdminSerializer includes all expected model fields."""
        serializer = serializers.QuestionAdminSerializer()
        fields = serializer.get_fields()

        # Check that all critical fields are available
        expected_fields = [
            "uuid",
            "description",
            "required",
            "question_type",
            "order",
            "user_guidance",
            "question_options",
            "allowed_file_types",
            "allowed_mime_types",
            "max_file_size_mb",
            "max_files_count",
            "min_value",
            "max_value",
            "operator",
            "review_answer_value",
            "always_requires_review",
            "guidance_answer_value",
            "guidance_operator",
            "always_show_guidance",
            "dependency_logic_operator",
            "url",
            "checklist_name",
            "checklist_uuid",
            "checklist",
        ]

        for field_name in expected_fields:
            self.assertIn(
                field_name,
                fields,
                f"Field '{field_name}' should be available in QuestionAdminSerializer",
            )

    def test_serializer_can_serialize_question_with_file_fields(self):
        """Test that serializer can serialize a question with file-related fields."""
        question = factories.QuestionFactory(
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf", ".doc"],
            allowed_mime_types=["application/pdf", "application/msword"],
            max_file_size_mb=10,
        )

        request = APIRequestFactory().get("/")
        request.user = self.fixture.staff
        context = {"request": request}
        serializer = serializers.QuestionAdminSerializer(question, context=context)
        data = serializer.data

        # Check that file fields are properly serialized
        self.assertEqual(data["allowed_file_types"], [".pdf", ".doc"])
        self.assertEqual(
            data["allowed_mime_types"], ["application/pdf", "application/msword"]
        )
        self.assertEqual(data["max_file_size_mb"], 10)
        self.assertEqual(data["question_type"], enums.QuestionTypes.FILE)

    def test_serializer_can_serialize_question_with_multiple_files_fields(self):
        """Test that serializer can serialize a question with multiple files fields."""
        question = factories.QuestionFactory(
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            allowed_file_types=[".jpg", ".png"],
            allowed_mime_types=["image/*"],
            max_file_size_mb=5,
            max_files_count=3,
        )

        request = APIRequestFactory().get("/")
        request.user = self.fixture.staff
        context = {"request": request}
        serializer = serializers.QuestionAdminSerializer(question, context=context)
        data = serializer.data

        # Check that all fields including max_files_count are properly serialized
        self.assertEqual(data["allowed_file_types"], [".jpg", ".png"])
        self.assertEqual(data["allowed_mime_types"], ["image/*"])
        self.assertEqual(data["max_file_size_mb"], 5)
        self.assertEqual(data["max_files_count"], 3)
        self.assertEqual(data["question_type"], enums.QuestionTypes.MULTIPLE_FILES)

    def test_serializer_can_serialize_question_with_number_fields(self):
        """Test that serializer can serialize a question with number validation fields."""
        question = factories.QuestionFactory(
            question_type=enums.QuestionTypes.NUMBER,
            min_value=1.0,
            max_value=100.0,
        )

        request = APIRequestFactory().get("/")
        request.user = self.fixture.staff
        context = {"request": request}
        serializer = serializers.QuestionAdminSerializer(question, context=context)
        data = serializer.data

        # Check that number validation fields are properly serialized
        self.assertEqual(float(data["min_value"]), 1.0)
        self.assertEqual(float(data["max_value"]), 100.0)
        self.assertEqual(data["question_type"], enums.QuestionTypes.NUMBER)


@ddt
class ChecklistQuestionsEndpointTest(test.APITestCase):
    """Test the checklist questions endpoint that was failing with the original error."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        # Create questions with various field types that were causing the issue
        self.file_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
            description="File upload question",
        )
        self.number_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=0.0,
            max_value=100.0,
            description="Number input question",
        )
        self.multiple_files_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.MULTIPLE_FILES,
            allowed_file_types=[".jpg", ".png"],
            max_files_count=5,
            description="Multiple files question",
        )

    def _get_questions_url(self, checklist=None):
        """Get the URL for the checklist questions endpoint."""
        if checklist is None:
            checklist = self.fixture.checklist
        return factories.ChecklistFactory.get_admin_url(checklist) + "questions/"

    @data("staff")
    def test_user_can_get_checklist_questions(self, user):
        """Test that the checklist questions endpoint works without serializer errors."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = self._get_questions_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Should return all questions for this checklist
        self.assertGreaterEqual(len(data), 3)  # At least our 3 created questions

        # Check that questions have the expected fields
        question_descriptions = {q["description"] for q in data}
        self.assertIn("File upload question", question_descriptions)
        self.assertIn("Number input question", question_descriptions)
        self.assertIn("Multiple files question", question_descriptions)

    @data("staff")
    def test_questions_endpoint_returns_file_fields(self, user):
        """Test that the questions endpoint properly returns file-related fields."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = self._get_questions_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Find the file question in the response
        file_question_data = next(
            (q for q in data if q["description"] == "File upload question"), None
        )
        self.assertIsNotNone(file_question_data)

        # Check that file fields are included and have correct values
        self.assertEqual(file_question_data["allowed_file_types"], [".pdf"])
        self.assertEqual(file_question_data["allowed_mime_types"], ["application/pdf"])
        self.assertEqual(file_question_data["max_file_size_mb"], 10)
        self.assertEqual(file_question_data["question_type"], enums.QuestionTypes.FILE)

    @data("staff")
    def test_questions_endpoint_returns_number_fields(self, user):
        """Test that the questions endpoint properly returns number validation fields."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = self._get_questions_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Find the number question in the response
        number_question_data = next(
            (q for q in data if q["description"] == "Number input question"), None
        )
        self.assertIsNotNone(number_question_data)

        # Check that number fields are included and have correct values
        self.assertEqual(float(number_question_data["min_value"]), 0.0)
        self.assertEqual(float(number_question_data["max_value"]), 100.0)
        self.assertEqual(
            number_question_data["question_type"], enums.QuestionTypes.NUMBER
        )

    @data("staff")
    def test_questions_endpoint_returns_multiple_files_fields(self, user):
        """Test that the questions endpoint properly returns multiple files fields."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = self._get_questions_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Find the multiple files question in the response
        multiple_files_question_data = next(
            (q for q in data if q["description"] == "Multiple files question"), None
        )
        self.assertIsNotNone(multiple_files_question_data)

        # Check that multiple files fields are included and have correct values
        self.assertEqual(
            multiple_files_question_data["allowed_file_types"], [".jpg", ".png"]
        )
        self.assertEqual(multiple_files_question_data["max_files_count"], 5)
        self.assertEqual(
            multiple_files_question_data["question_type"],
            enums.QuestionTypes.MULTIPLE_FILES,
        )

    @data("owner")
    def test_unauthorized_user_cannot_access_questions_endpoint(self, user):
        """Test that unauthorized users cannot access the questions endpoint."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = self._get_questions_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff")
    def test_questions_endpoint_with_pagination(self, user):
        """Test that the questions endpoint works with pagination parameters."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = self._get_questions_url()
        response = self.client.get(url, {"page": 1, "page_size": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should not fail with the original field error


class QuestionAdminPatchWhitelistTest(test.APITestCase):
    """Regression tests: PATCH must apply the per-type whitelists using the
    persisted question_type when the payload doesn't include question_type."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.checklist = factories.ChecklistFactory()
        self.text_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.TEXT_INPUT,
        )
        self.file_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.FILE,
        )
        self.url = factories.QuestionFactory.get_admin_url(self.text_question)

    def test_patch_min_value_on_non_numeric_question_rejected(self):
        response = self.client.patch(self.url, {"min_value": 10}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Min and max values", str(response.data))

    def test_patch_max_value_on_non_numeric_question_rejected(self):
        response = self.client.patch(self.url, {"max_value": 100}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Min and max values", str(response.data))

    def test_patch_allowed_file_types_on_non_file_question_rejected(self):
        response = self.client.patch(
            self.url, {"allowed_file_types": [".pdf"]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("File validation fields", str(response.data))

    def test_patch_max_file_size_mb_on_non_file_question_rejected(self):
        response = self.client.patch(self.url, {"max_file_size_mb": 10}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("File validation fields", str(response.data))

    def test_patch_max_files_count_on_file_question_rejected(self):
        # max_files_count is only valid for MULTIPLE_FILES, not FILE.
        # PATCH on an existing FILE question must still reject it.
        url = factories.QuestionFactory.get_admin_url(self.file_question)
        response = self.client.patch(url, {"max_files_count": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("MULTIPLE_FILES", str(response.data))

    def test_patch_min_value_on_number_question_still_works(self):
        number_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
        )
        url = factories.QuestionFactory.get_admin_url(number_question)
        response = self.client.patch(url, {"min_value": 10}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        number_question.refresh_from_db()
        self.assertEqual(number_question.min_value, 10)
