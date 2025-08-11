"""Tests for conditional user guidance functionality."""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class ConditionalUserGuidanceModelTest(test.APITransactionTestCase):
    """Test the model-level conditional user guidance functionality."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = self.fixture.question

    def test_always_show_guidance_default_true(self):
        """Test that always_show_guidance defaults to True."""
        question = factories.QuestionFactory()
        self.assertTrue(question.always_show_guidance)

    def test_should_show_guidance_with_always_show_true(self):
        """Test guidance shows when always_show_guidance=True regardless of answer."""
        self.question.user_guidance = "Test guidance"
        self.question.always_show_guidance = True
        self.question.save()

        # Should show for any answer when always_show_guidance=True
        self.assertTrue(self.question.should_show_guidance(True))
        self.assertTrue(self.question.should_show_guidance(False))
        self.assertTrue(self.question.should_show_guidance("any text"))
        self.assertTrue(self.question.should_show_guidance(None))

    def test_should_show_guidance_with_empty_guidance_text(self):
        """Test guidance doesn't show when guidance text is empty."""
        self.question.user_guidance = ""
        self.question.always_show_guidance = True
        self.question.save()

        self.assertFalse(self.question.should_show_guidance(True))

    def test_should_show_guidance_with_whitespace_only(self):
        """Test guidance doesn't show when guidance text is only whitespace."""
        self.question.user_guidance = "   \n\t   "
        self.question.always_show_guidance = True
        self.question.save()

        self.assertFalse(self.question.should_show_guidance(True))

    def test_conditional_guidance_equals_operator(self):
        """Test conditional guidance with equals operator."""
        self.question.user_guidance = "Test conditional guidance"
        self.question.always_show_guidance = False
        self.question.guidance_answer_value = True
        self.question.guidance_operator = "equals"
        self.question.save()

        # Should show only for matching answer
        self.assertTrue(self.question.should_show_guidance(True))
        self.assertFalse(self.question.should_show_guidance(False))
        self.assertFalse(self.question.should_show_guidance("true"))

    def test_conditional_guidance_contains_operator(self):
        """Test conditional guidance with contains operator."""
        self.question.user_guidance = "Security guidance"
        self.question.always_show_guidance = False
        self.question.guidance_answer_value = [
            "security"
        ]  # Contains operator expects a list
        self.question.guidance_operator = "contains"
        self.question.save()

        self.assertTrue(
            self.question.should_show_guidance("high security requirements")
        )
        self.assertTrue(
            self.question.should_show_guidance("security is important")
        )  # Case sensitive
        self.assertFalse(self.question.should_show_guidance("compliance requirements"))

    def test_conditional_guidance_in_operator(self):
        """Test conditional guidance with in operator."""
        self.question.user_guidance = "High-risk project guidance"
        self.question.always_show_guidance = False
        self.question.guidance_answer_value = ["high", "critical", "enterprise"]
        self.question.guidance_operator = "in"
        self.question.save()

        # For "in" operator, user_answer must be a list when required_value is a list
        self.assertTrue(self.question.should_show_guidance(["high"]))
        self.assertTrue(self.question.should_show_guidance(["critical"]))
        self.assertFalse(self.question.should_show_guidance(["low"]))
        self.assertFalse(self.question.should_show_guidance(["medium"]))

    def test_conditional_guidance_not_in_operator(self):
        """Test conditional guidance with not_in operator."""
        self.question.user_guidance = "Standard project guidance"
        self.question.always_show_guidance = False
        self.question.guidance_answer_value = ["high", "critical"]
        self.question.guidance_operator = "not_in"
        self.question.save()

        # For "not_in" operator, user_answer must be a list when required_value is a list
        self.assertTrue(self.question.should_show_guidance(["low"]))
        self.assertTrue(self.question.should_show_guidance(["medium"]))
        self.assertFalse(self.question.should_show_guidance(["high"]))
        self.assertFalse(self.question.should_show_guidance(["critical"]))

    def test_conditional_guidance_no_conditions_set(self):
        """Test that guidance doesn't show when conditions aren't properly set."""
        self.question.user_guidance = "Test guidance"
        self.question.always_show_guidance = False
        # Don't set guidance_answer_value or guidance_operator
        self.question.save()

        self.assertFalse(self.question.should_show_guidance(True))
        self.assertFalse(self.question.should_show_guidance("anything"))


@ddt
class UserGuidanceAPITest(test.APITransactionTestCase):
    """Test conditional user guidance via REST API."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.checklist = factories.ChecklistFactory()
        self.url = factories.QuestionFactory.get_admin_list_url()

    def _get_base_payload(self):
        """Get base question payload."""
        return {
            "description": "Test question with guidance",
            "question_type": enums.QuestionTypes.BOOLEAN,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": True,
            "order": 1,
        }

    @data("staff")
    def test_create_question_with_always_visible_guidance(self, user):
        """Test creating question with always-visible guidance."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "user_guidance": "This guidance always shows",
                "always_show_guidance": True,
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(
            description="Test question with guidance"
        )
        self.assertEqual(question.user_guidance, "This guidance always shows")
        self.assertTrue(question.always_show_guidance)

    @data("staff")
    def test_create_question_with_conditional_guidance(self, user):
        """Test creating question with conditional guidance."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "user_guidance": "This shows only for 'Yes' answers",
                "always_show_guidance": False,
                "guidance_answer_value": True,
                "guidance_operator": "equals",
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(
            description="Test question with guidance"
        )
        self.assertEqual(question.user_guidance, "This shows only for 'Yes' answers")
        self.assertFalse(question.always_show_guidance)
        self.assertEqual(question.guidance_answer_value, True)
        self.assertEqual(question.guidance_operator, "equals")

    @data("staff")
    def test_validation_conditional_guidance_requires_both_fields(self, user):
        """Test validation that conditional guidance requires both operator and value."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Test missing guidance_operator
        payload = self._get_base_payload()
        payload.update(
            {
                "user_guidance": "Test guidance",
                "always_show_guidance": False,
                "guidance_answer_value": True,
                # Missing guidance_operator
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("guidance_operator", str(response.content))

        # Test missing guidance_answer_value
        payload = self._get_base_payload()
        payload.update(
            {
                "user_guidance": "Test guidance",
                "always_show_guidance": False,
                "guidance_operator": "equals",
                # Missing guidance_answer_value
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("guidance_operator", str(response.content))

    @data("staff")
    def test_validation_invalid_guidance_operator_for_question_type(self, user):
        """Test validation of guidance operator compatibility with question type."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "question_type": enums.QuestionTypes.BOOLEAN,
                "user_guidance": "Test guidance",
                "always_show_guidance": False,
                "guidance_answer_value": "some text",
                "guidance_operator": "contains",  # Invalid for boolean
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Guidance operator", str(response.content))

    @data("staff")
    def test_validation_invalid_guidance_answer_value_for_question_type(self, user):
        """Test validation of guidance answer value compatibility with question type."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "question_type": enums.QuestionTypes.BOOLEAN,
                "user_guidance": "Test guidance",
                "always_show_guidance": False,
                "guidance_answer_value": ["list", "values"],  # Invalid for boolean
                "guidance_operator": "equals",
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Guidance answer value", str(response.content))

    @data("staff")
    def test_update_guidance_configuration(self, user):
        """Test updating guidance configuration on existing question."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Create question first
        question = factories.QuestionFactory(checklist=self.checklist)
        url = factories.QuestionFactory.get_admin_url(question)

        # Update with conditional guidance
        payload = {
            "user_guidance": "Updated guidance for enterprise projects",
            "always_show_guidance": False,
            "guidance_answer_value": "enterprise",
            "guidance_operator": "equals",
        }

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        question.refresh_from_db()
        self.assertEqual(
            question.user_guidance, "Updated guidance for enterprise projects"
        )
        self.assertFalse(question.always_show_guidance)
        self.assertEqual(question.guidance_answer_value, "enterprise")
        self.assertEqual(question.guidance_operator, "equals")

    @data("staff")
    def test_remove_guidance_conditions(self, user):
        """Test removing guidance conditions to make guidance always visible."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Create question with conditional guidance
        question = factories.QuestionFactory(
            checklist=self.checklist,
            user_guidance="Test guidance",
            always_show_guidance=False,
            guidance_answer_value="test",
            guidance_operator="equals",
        )
        url = factories.QuestionFactory.get_admin_url(question)

        # Update to make guidance always visible
        payload = {
            "always_show_guidance": True,
            "guidance_answer_value": [],
            "guidance_operator": "equals",
        }

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        question.refresh_from_db()
        self.assertTrue(question.always_show_guidance)


@ddt
class UserGuidanceComplexScenariosTest(test.APITransactionTestCase):
    """Test complex user guidance scenarios from documentation."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.checklist = factories.ChecklistFactory()

    @data("staff")
    def test_gdpr_compliance_guidance_scenario(self, user):
        """Test GDPR compliance guidance scenario from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "Will you be processing EU citizen data?",
            "question_type": enums.QuestionTypes.BOOLEAN,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "user_guidance": "Since you're processing EU data, you must comply with GDPR requirements. Please review our GDPR compliance checklist.",
            "always_show_guidance": False,
            "guidance_answer_value": True,
            "guidance_operator": "equals",
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(
            description="Will you be processing EU citizen data?"
        )

        # Test guidance logic
        self.assertTrue(question.should_show_guidance(True))  # Shows for Yes
        self.assertFalse(question.should_show_guidance(False))  # Hidden for No

    @data("staff")
    def test_multi_select_warning_guidance_scenario(self, user):
        """Test multi-select warning guidance scenario from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Create the question first without guidance, then update with guidance
        # This avoids validation issues with options not existing yet
        question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Which data types will you collect?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=1,
        )

        # Create options for the question
        personal_option = factories.QuestionOptionFactory(
            question=question, label="Personal Data", order=1
        )
        financial_option = factories.QuestionOptionFactory(
            question=question, label="Financial Data", order=2
        )
        health_option = factories.QuestionOptionFactory(
            question=question, label="Health Data", order=3
        )
        public_option = factories.QuestionOptionFactory(
            question=question, label="Public Data", order=4
        )

        # Update question with guidance using option UUIDs
        question.user_guidance = "You've selected multiple sensitive data types. Additional security measures and approvals may be required."
        question.always_show_guidance = False
        question.guidance_answer_value = [
            str(personal_option.uuid),
            str(financial_option.uuid),
            str(health_option.uuid),
        ]
        question.guidance_operator = "in"
        question.save()

        # Test guidance logic - multi-select requires lists
        self.assertTrue(question.should_show_guidance([str(personal_option.uuid)]))
        self.assertTrue(question.should_show_guidance([str(financial_option.uuid)]))
        self.assertFalse(question.should_show_guidance([str(public_option.uuid)]))

    @data("staff")
    def test_technical_guidance_for_ai_ml_scenario(self, user):
        """Test technical guidance for AI/ML scenario from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "Which technologies will you use?",
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": True,
            "order": 1,
            "user_guidance": "Since you're using AI/ML technologies, additional ethical review and bias testing may be required. Please consult with our AI Ethics team.",
            "always_show_guidance": False,
            "guidance_answer_value": [
                "machine_learning",
                "artificial_intelligence",
                "deep_learning",
            ],
            "guidance_operator": "contains",
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(
            description="Which technologies will you use?"
        )

        # Test guidance logic - text input with contains operator
        self.assertTrue(question.should_show_guidance("machine_learning"))
        self.assertTrue(question.should_show_guidance("artificial_intelligence"))
        self.assertFalse(question.should_show_guidance("database"))
        self.assertFalse(question.should_show_guidance("web_framework"))

    @data("staff")
    def test_enterprise_scale_guidance_scenario(self, user):
        """Test enterprise scale guidance scenario from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "How many users will access this system?",
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": True,
            "order": 1,
            "user_guidance": "For enterprise-scale deployments, you'll need to complete additional capacity planning and load testing requirements. Please coordinate with the Infrastructure team.",
            "always_show_guidance": False,
            "guidance_answer_value": ["1000_plus", "enterprise"],
            "guidance_operator": "contains",
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(
            description="How many users will access this system?"
        )

        # Test guidance logic - text input with contains operator
        self.assertTrue(question.should_show_guidance("1000_plus"))
        self.assertTrue(question.should_show_guidance("enterprise"))
        self.assertFalse(question.should_show_guidance("small"))
        self.assertFalse(question.should_show_guidance("medium"))
