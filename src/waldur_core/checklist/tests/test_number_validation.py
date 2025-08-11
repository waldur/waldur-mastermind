"""Tests for number validation with min/max limits."""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class NumberValidationModelTest(test.APITransactionTestCase):
    """Test the model-level number validation functionality."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def test_number_question_without_limits_accepts_any_number(self):
        """Test that NUMBER questions without min/max accept any valid number."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=None,
            max_value=None,
        )

        # Should accept various number formats (only int/float per current validation logic)
        self.assertTrue(question.is_valid_answer(42))
        self.assertTrue(question.is_valid_answer(42.5))
        self.assertTrue(question.is_valid_answer(-100))
        self.assertTrue(question.is_valid_answer(0))
        # Note: String numbers not accepted per current validation logic
        self.assertFalse(question.is_valid_answer("42"))
        self.assertFalse(question.is_valid_answer("42.5"))

    def test_number_question_with_min_value_validation(self):
        """Test that NUMBER questions respect min_value constraint."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=10,
            max_value=None,
        )

        # Should accept values >= min_value
        self.assertTrue(question.is_valid_answer(10))
        self.assertTrue(question.is_valid_answer(15))
        self.assertTrue(question.is_valid_answer(10.5))
        self.assertTrue(question.is_valid_answer(1000))

        # Should reject values < min_value
        self.assertFalse(question.is_valid_answer(9))
        self.assertFalse(question.is_valid_answer(9.99))
        self.assertFalse(question.is_valid_answer(-5))

        # String values not accepted per current validation logic
        self.assertFalse(question.is_valid_answer("12"))
        self.assertFalse(question.is_valid_answer("5"))

    def test_number_question_with_max_value_validation(self):
        """Test that NUMBER questions respect max_value constraint."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=None,
            max_value=100,
        )

        # Should accept values <= max_value
        self.assertTrue(question.is_valid_answer(100))
        self.assertTrue(question.is_valid_answer(50))
        self.assertTrue(question.is_valid_answer(0.5))
        self.assertTrue(question.is_valid_answer(-50))

        # Should reject values > max_value
        self.assertFalse(question.is_valid_answer(101))
        self.assertFalse(question.is_valid_answer(100.1))
        self.assertFalse(question.is_valid_answer(1000))

        # String values not accepted per current validation logic
        self.assertFalse(question.is_valid_answer("99"))
        self.assertFalse(question.is_valid_answer("150"))

    def test_number_question_with_both_min_max_validation(self):
        """Test that NUMBER questions respect both min and max constraints."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=10,
            max_value=50,
        )

        # Should accept values within range
        self.assertTrue(question.is_valid_answer(10))
        self.assertTrue(question.is_valid_answer(25))
        self.assertTrue(question.is_valid_answer(50))
        self.assertTrue(question.is_valid_answer(10.5))

        # Should reject values outside range
        self.assertFalse(question.is_valid_answer(9))
        self.assertFalse(question.is_valid_answer(51))
        self.assertFalse(question.is_valid_answer(9.99))

        # String values not accepted per current validation logic
        self.assertFalse(question.is_valid_answer("30"))
        self.assertFalse(question.is_valid_answer("5"))
        self.assertFalse(question.is_valid_answer("60"))

    def test_decimal_min_max_values(self):
        """Test that decimal min/max values work correctly."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=10.5,
            max_value=20.75,
        )

        # Should accept values within decimal range
        self.assertTrue(question.is_valid_answer(10.5))
        self.assertTrue(question.is_valid_answer(15.25))
        self.assertTrue(question.is_valid_answer(20.75))

        # Should reject values outside decimal range
        self.assertFalse(question.is_valid_answer(10.49))
        self.assertFalse(question.is_valid_answer(20.76))

        # String values not accepted per current validation logic
        self.assertFalse(question.is_valid_answer("12.3"))
        self.assertFalse(question.is_valid_answer("10.4"))

    def test_invalid_number_formats_rejected(self):
        """Test that invalid number formats are rejected even with limits."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=0,
            max_value=100,
        )

        # Should reject non-numeric values
        self.assertFalse(question.is_valid_answer("not a number"))
        self.assertFalse(question.is_valid_answer("12.5.3"))
        self.assertFalse(question.is_valid_answer("twelve"))
        self.assertFalse(question.is_valid_answer([]))
        self.assertFalse(question.is_valid_answer({}))

    def test_non_number_questions_ignore_min_max(self):
        """Test that non-NUMBER questions ignore min/max values."""
        # TEXT_INPUT question with min/max values should ignore them
        text_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.TEXT_INPUT,
            min_value=10,
            max_value=50,
        )

        # Should accept any valid text regardless of min/max
        self.assertTrue(text_question.is_valid_answer("short"))
        self.assertTrue(text_question.is_valid_answer("a very long text string"))
        self.assertTrue(text_question.is_valid_answer("42"))  # Number as text

        # BOOLEAN question with min/max values should ignore them
        boolean_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.BOOLEAN,
            min_value=10,
            max_value=50,
        )

        self.assertTrue(boolean_question.is_valid_answer(True))
        self.assertTrue(boolean_question.is_valid_answer(False))

    def test_required_number_with_limits(self):
        """Test required number questions with min/max limits."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            required=True,
            min_value=1,
            max_value=10,
        )

        # Should reject None for required field
        self.assertFalse(question.is_valid_answer(None))

        # Should accept valid values in range
        self.assertTrue(question.is_valid_answer(5))

        # Should reject values outside range
        self.assertFalse(question.is_valid_answer(0))
        self.assertFalse(question.is_valid_answer(11))


@ddt
class NumberValidationSerializerTest(test.APITransactionTestCase):
    """Test number validation via serializers and API."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.checklist = factories.ChecklistFactory()
        self.url = factories.QuestionFactory.get_admin_list_url()

    def _get_base_payload(self):
        """Get base question payload."""
        return {
            "description": "Enter a number",
            "question_type": enums.QuestionTypes.NUMBER,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": False,
            "order": 1,
        }

    @data("staff")
    def test_create_number_question_with_min_max(self, user):
        """Test creating NUMBER question with min/max values."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "min_value": "10.5",
                "max_value": "100.75",
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(description="Enter a number")
        self.assertEqual(float(question.min_value), 10.5)
        self.assertEqual(float(question.max_value), 100.75)

    @data("staff")
    def test_create_number_question_with_only_min(self, user):
        """Test creating NUMBER question with only min_value."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "min_value": "0",
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(description="Enter a number")
        self.assertEqual(float(question.min_value), 0)
        self.assertIsNone(question.max_value)

    @data("staff")
    def test_create_number_question_with_only_max(self, user):
        """Test creating NUMBER question with only max_value."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "max_value": "1000",
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(description="Enter a number")
        self.assertIsNone(question.min_value)
        self.assertEqual(float(question.max_value), 1000)

    @data("staff")
    def test_validation_min_greater_than_max_rejected(self, user):
        """Test validation rejects when min > max."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "min_value": "100",
                "max_value": "50",  # Less than min
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Minimum value cannot be greater than maximum value", str(response.content)
        )

    @data("staff")
    def test_validation_min_max_only_for_number_questions(self, user):
        """Test validation rejects min/max for non-NUMBER questions."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Try to set min/max on TEXT_INPUT question
        payload = self._get_base_payload()
        payload.update(
            {
                "question_type": enums.QuestionTypes.TEXT_INPUT,
                "min_value": "10",
                "max_value": "100",
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Min and max values can only be set for NUMBER type questions",
            str(response.content),
        )

    @data("staff")
    def test_update_existing_question_with_limits(self, user):
        """Test updating existing question to add min/max limits."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Create question first
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
        )
        url = factories.QuestionFactory.get_admin_url(question)

        # Update with min/max values
        payload = {
            "min_value": "5.5",
            "max_value": "95.25",
        }

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        question.refresh_from_db()
        self.assertEqual(float(question.min_value), 5.5)
        self.assertEqual(float(question.max_value), 95.25)

    @data("staff")
    def test_remove_limits_from_question(self, user):
        """Test removing min/max limits from existing question."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Create question with limits
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=10,
            max_value=50,
        )
        url = factories.QuestionFactory.get_admin_url(question)

        # Remove limits by setting them to null
        payload = {
            "min_value": None,
            "max_value": None,
        }

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        question.refresh_from_db()
        self.assertIsNone(question.min_value)
        self.assertIsNone(question.max_value)


@ddt
class NumberValidationAnswerSubmissionTest(test.APITransactionTestCase):
    """Test number validation during answer submission."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.checklist = factories.ChecklistFactory()

        # Create completion
        self.completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

    def test_submit_valid_answer_within_limits(self):
        """Test submitting valid answers within min/max limits."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=0,
            max_value=100,
        )

        # Test various valid values (only int/float per current validation logic)
        valid_values = [0, 50, 100, 25.5, 99.9]

        for value in valid_values:
            with self.subTest(value=value):
                answer = models.Answer(
                    question=question,
                    user=self.fixture.admin,
                    completion=self.completion,
                    answer_data=value,
                )
                # Should not raise validation error
                self.assertTrue(question.is_valid_answer(value))
                answer.save()  # Should succeed
                answer.delete()  # Clean up for next test

    def test_submit_invalid_answer_outside_limits(self):
        """Test that invalid answers outside limits are rejected."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=10,
            max_value=50,
        )

        # Test various invalid values
        invalid_values = [9, 51, -5, 151]

        for value in invalid_values:
            with self.subTest(value=value):
                # Should fail validation
                self.assertFalse(question.is_valid_answer(value))

    def test_serializer_exposes_min_max_to_frontend(self):
        """Test that serializers expose min/max values for UI form generation."""
        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.NUMBER,
            min_value=1,
            max_value=10,
        )

        # Test admin serializer includes min/max
        from rest_framework.test import APIRequestFactory

        from waldur_core.checklist.serializers import QuestionAdminSerializer

        request = APIRequestFactory().get("/")
        request.user = self.fixture.admin
        context = {"request": request}

        serializer = QuestionAdminSerializer(question, context=context)
        data = serializer.data

        self.assertIn("min_value", data)
        self.assertIn("max_value", data)
        self.assertEqual(str(data["min_value"]), "1.0000")
        self.assertEqual(str(data["max_value"]), "10.0000")

        # Test user-facing serializer also includes min/max
        from waldur_core.checklist.serializers import QuestionWithAnswerSerializer

        context = {"completion": self.completion, "request": request}
        user_serializer = QuestionWithAnswerSerializer(question, context=context)
        user_data = user_serializer.data

        self.assertIn("min_value", user_data)
        self.assertIn("max_value", user_data)


@ddt
class NumberValidationIntegrationTest(test.APITransactionTestCase):
    """Integration tests for number validation in real workflow scenarios."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.checklist = factories.ChecklistFactory()

    def test_budget_validation_scenario(self):
        """Test realistic budget validation scenario."""
        # Create budget question with reasonable limits
        budget_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What is your project budget? (in thousands)",
            question_type=enums.QuestionTypes.NUMBER,
            required=True,
            min_value=1,  # At least $1k
            max_value=10000,  # Max $10M
            user_guidance="Please enter your budget in thousands of dollars (e.g., 100 for $100,000)",
        )

        # Test valid budget values
        valid_budgets = [1, 50, 1000, 5000, 10000, 250.5]
        for budget in valid_budgets:
            with self.subTest(budget=budget):
                self.assertTrue(budget_question.is_valid_answer(budget))

        # Test invalid budget values
        invalid_budgets = [0, -10, 10001, 50000]
        for budget in invalid_budgets:
            with self.subTest(budget=budget):
                self.assertFalse(budget_question.is_valid_answer(budget))

    def test_percentage_validation_scenario(self):
        """Test percentage validation scenario."""
        # Create percentage question
        percentage_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What percentage of completion do you expect?",
            question_type=enums.QuestionTypes.NUMBER,
            required=True,
            min_value=0,
            max_value=100,
            user_guidance="Enter a percentage between 0 and 100",
        )

        # Test valid percentages
        valid_percentages = [0, 25, 50, 75, 100, 33.33, 66.67]
        for percentage in valid_percentages:
            with self.subTest(percentage=percentage):
                self.assertTrue(percentage_question.is_valid_answer(percentage))

        # Test invalid percentages
        invalid_percentages = [-1, 101, 150, -50]
        for percentage in invalid_percentages:
            with self.subTest(percentage=percentage):
                self.assertFalse(percentage_question.is_valid_answer(percentage))

    def test_age_validation_scenario(self):
        """Test age validation scenario with reasonable human age limits."""
        age_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What is the age of the primary contact?",
            question_type=enums.QuestionTypes.NUMBER,
            required=False,
            min_value=18,  # Minimum working age
            max_value=100,  # Maximum reasonable age
        )

        # Test valid ages
        valid_ages = [18, 25, 45, 65, 80, 100]
        for age in valid_ages:
            with self.subTest(age=age):
                self.assertTrue(age_question.is_valid_answer(age))

        # Test invalid ages
        invalid_ages = [17, 101, 150, 0, -5]
        for age in invalid_ages:
            with self.subTest(age=age):
                self.assertFalse(age_question.is_valid_answer(age))

    def test_scientific_measurement_scenario(self):
        """Test scientific measurement with decimal precision."""
        measurement_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Enter measurement value (in micrometers)",
            question_type=enums.QuestionTypes.NUMBER,
            required=True,
            min_value=0.001,  # 1 nanometer minimum
            max_value=1000000,  # 1 meter maximum
        )

        # Test valid measurements
        valid_measurements = [0.001, 0.5, 10.25, 1000, 50000, 1000000]
        for measurement in valid_measurements:
            with self.subTest(measurement=measurement):
                self.assertTrue(measurement_question.is_valid_answer(measurement))

        # Test invalid measurements
        invalid_measurements = [0.0009, 1000001, -1]
        for measurement in invalid_measurements:
            with self.subTest(measurement=measurement):
                self.assertFalse(measurement_question.is_valid_answer(measurement))
