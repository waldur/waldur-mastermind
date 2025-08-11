"""Tests for advanced review trigger functionality."""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class ReviewTriggerModelTest(test.APITransactionTestCase):
    """Test the model-level review trigger functionality."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = self.fixture.question

    def test_always_requires_review_functionality(self):
        """Test always_requires_review behavior."""
        self.question.always_requires_review = True
        self.question.save()

        # Should trigger review for any answer
        self.assertTrue(self.question.should_trigger_review(True))
        self.assertTrue(self.question.should_trigger_review(False))
        self.assertTrue(self.question.should_trigger_review("any text"))
        self.assertTrue(self.question.should_trigger_review(None))

    def test_conditional_review_trigger_equals(self):
        """Test conditional review trigger with equals operator."""
        self.question.always_requires_review = False
        self.question.review_answer_value = "high"
        self.question.operator = "equals"
        self.question.save()

        self.assertTrue(self.question.should_trigger_review("high"))
        self.assertFalse(self.question.should_trigger_review("low"))
        self.assertFalse(self.question.should_trigger_review("medium"))

    def test_conditional_review_trigger_contains(self):
        """Test conditional review trigger with contains operator."""
        self.question.always_requires_review = False
        self.question.review_answer_value = ["sensitive"]
        self.question.operator = "contains"
        self.question.save()

        self.assertTrue(self.question.should_trigger_review("contains sensitive data"))
        self.assertTrue(self.question.should_trigger_review("sensitive information"))
        self.assertFalse(self.question.should_trigger_review("public information"))

    def test_conditional_review_trigger_in(self):
        """Test conditional review trigger with in operator."""
        self.question.question_type = enums.QuestionTypes.SINGLE_SELECT
        self.question.always_requires_review = False
        self.question.review_answer_value = ["high", "critical", "severe"]
        self.question.operator = "in"
        self.question.save()

        self.assertTrue(self.question.should_trigger_review(["high"]))
        self.assertTrue(self.question.should_trigger_review(["critical"]))
        self.assertFalse(self.question.should_trigger_review(["low"]))

    def test_conditional_review_trigger_not_in(self):
        """Test conditional review trigger with not_in operator."""
        self.question.question_type = enums.QuestionTypes.SINGLE_SELECT
        self.question.always_requires_review = False
        self.question.review_answer_value = ["low", "minimal"]
        self.question.operator = "not_in"
        self.question.save()

        self.assertTrue(self.question.should_trigger_review(["high"]))
        self.assertTrue(self.question.should_trigger_review(["medium"]))
        self.assertFalse(self.question.should_trigger_review(["low"]))
        self.assertFalse(self.question.should_trigger_review(["minimal"]))

    def test_no_review_trigger_when_conditions_not_met(self):
        """Test that no review is triggered when conditions aren't met."""
        self.question.always_requires_review = False
        self.question.review_answer_value = "high"
        self.question.operator = "equals"
        self.question.save()

        self.assertFalse(self.question.should_trigger_review("low"))
        self.assertFalse(self.question.should_trigger_review("medium"))


@ddt
class ReviewTriggerAPITest(test.APITransactionTestCase):
    """Test review trigger configuration via REST API."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.checklist = factories.ChecklistFactory()
        self.url = factories.QuestionFactory.get_admin_list_url()

    def _get_base_payload(self):
        """Get base question payload."""
        return {
            "description": "Test question with review triggers",
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": True,
            "order": 1,
        }

    @data("staff")
    def test_create_question_with_always_requires_review(self, user):
        """Test creating question that always requires review."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update({"always_requires_review": True})

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(
            description="Test question with review triggers"
        )
        self.assertTrue(question.always_requires_review)

    @data("staff")
    def test_create_question_with_conditional_review_trigger(self, user):
        """Test creating question with conditional review trigger."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        payload = self._get_base_payload()
        payload.update(
            {
                "always_requires_review": False,
                "review_answer_value": ["high", "critical"],
                "operator": "contains",
            }
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(
            description="Test question with review triggers"
        )
        self.assertFalse(question.always_requires_review)
        self.assertEqual(question.review_answer_value, ["high", "critical"])
        self.assertEqual(question.operator, "contains")

    @data("staff")
    def test_update_review_trigger_configuration(self, user):
        """Test updating review trigger configuration."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        # Create question first
        question = factories.QuestionFactory(checklist=self.checklist)
        url = factories.QuestionFactory.get_admin_url(question)

        # Update with review trigger
        payload = {
            "review_answer_value": ["enterprise", "large_scale"],
            "operator": "in",
            "always_requires_review": False,
        }

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        question.refresh_from_db()
        self.assertEqual(question.review_answer_value, ["enterprise", "large_scale"])
        self.assertEqual(question.operator, "in")
        self.assertFalse(question.always_requires_review)


@ddt
class CombinedReviewAndGuidanceTest(test.APITransactionTestCase):
    """Test questions that combine review triggers and user guidance."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.checklist = factories.ChecklistFactory()

    @data("staff")
    def test_financial_transaction_combined_workflow(self, user):
        """Test financial transaction scenario with both guidance and review trigger."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "Will your application process financial transactions?",
            "question_type": enums.QuestionTypes.BOOLEAN,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": True,
            "order": 1,
            # User guidance configuration
            "user_guidance": "Financial transaction processing requires PCI DSS compliance. Please review our payment processing guidelines and ensure all credit card data is properly secured.",
            "always_show_guidance": False,
            "guidance_answer_value": True,
            "guidance_operator": "equals",
            # Review trigger configuration (same condition)
            "review_answer_value": True,
            "operator": "equals",
            "always_requires_review": False,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(
            description="Will your application process financial transactions?"
        )

        # Test both guidance and review trigger work for the same condition
        self.assertTrue(question.should_show_guidance(True))
        self.assertTrue(question.should_trigger_review(True))

        self.assertFalse(question.should_show_guidance(False))
        self.assertFalse(question.should_trigger_review(False))

    @data("staff")
    def test_multi_condition_security_workflow(self, user):
        """Test multi-condition security workflow with different conditions for guidance vs review."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "Select all data types you'll be handling:",
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "required": True,
            # Guidance for any sensitive data
            "user_guidance": "You've selected sensitive data types. Additional security measures, encryption, and audit logging will be required. Please coordinate with the Security team early in your project.",
            "always_show_guidance": False,
            "guidance_answer_value": [
                "personal_data",
                "financial_data",
                "health_data",
                "confidential",
            ],
            "guidance_operator": "contains",
            # Review trigger for high-risk combinations only
            "review_answer_value": ["financial_data", "health_data"],
            "operator": "contains",
            "always_requires_review": False,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        question = models.Question.objects.get(
            description="Select all data types you'll be handling:"
        )

        # Test different conditions
        # Personal data: shows guidance but doesn't trigger review
        self.assertTrue(question.should_show_guidance("personal_data"))
        self.assertFalse(question.should_trigger_review("personal_data"))

        # Financial data: shows guidance AND triggers review
        self.assertTrue(question.should_show_guidance("financial_data"))
        self.assertTrue(question.should_trigger_review("financial_data"))

        # Public data: neither guidance nor review
        self.assertFalse(question.should_show_guidance("public_data"))
        self.assertFalse(question.should_trigger_review("public_data"))


@ddt
class ReviewTriggerDocumentationScenariosTest(test.APITransactionTestCase):
    """Test review trigger scenarios from documentation."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.checklist = factories.ChecklistFactory()

    @data("staff")
    def test_security_review_for_high_risk_projects(self, user):
        """Test security review scenario from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "What is your project's risk level?",
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "review_answer_value": ["high", "critical"],
            "operator": "contains",
            "always_requires_review": False,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(
            description="What is your project's risk level?"
        )

        self.assertTrue(question.should_trigger_review("high"))
        self.assertTrue(question.should_trigger_review("critical"))
        self.assertFalse(question.should_trigger_review("low"))
        self.assertFalse(question.should_trigger_review("medium"))

    @data("staff")
    def test_budget_review_for_large_expenditures(self, user):
        """Test budget review scenario from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "Select your budget range:",
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "review_answer_value": ["over_100k"],
            "operator": "contains",
            "always_requires_review": False,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(description="Select your budget range:")

        self.assertTrue(question.should_trigger_review("over_100k"))
        self.assertFalse(question.should_trigger_review("under_10k"))
        self.assertFalse(question.should_trigger_review("10k_to_50k"))

    @data("staff")
    def test_compliance_review_for_text_content(self, user):
        """Test compliance review for specific text content from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "Describe your data handling procedures:",
            "question_type": enums.QuestionTypes.TEXT_AREA,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "review_answer_value": ["export"],
            "operator": "contains",
            "always_requires_review": False,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(
            description="Describe your data handling procedures:"
        )

        self.assertTrue(
            question.should_trigger_review("We will export data to third parties")
        )
        self.assertTrue(
            question.should_trigger_review("Data export procedures are documented")
        )
        self.assertFalse(question.should_trigger_review("All data remains internal"))

    @data("staff")
    def test_multiple_compliance_frameworks_review(self, user):
        """Test multiple compliance frameworks review scenario from documentation."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)

        url = factories.QuestionFactory.get_admin_list_url()
        payload = {
            "description": "Which compliance frameworks apply?",
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "review_answer_value": ["gdpr", "hipaa", "sox", "pci_dss"],
            "operator": "contains",
            "always_requires_review": False,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        question = models.Question.objects.get(
            description="Which compliance frameworks apply?"
        )

        self.assertTrue(question.should_trigger_review("gdpr"))
        self.assertTrue(question.should_trigger_review("hipaa"))
        self.assertTrue(question.should_trigger_review("sox"))
        self.assertTrue(question.should_trigger_review("pci_dss"))
        self.assertFalse(question.should_trigger_review("internal_only"))
        self.assertFalse(question.should_trigger_review("none"))


@ddt
class AnswerReviewFlagTest(test.APITransactionTestCase):
    """Test that Answer objects are correctly flagged for review when saved."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = self.fixture.question

    def test_answer_flagged_for_review_when_condition_met(self):
        """Test that answers are flagged for review when trigger conditions are met."""
        # Configure question to trigger review for "high" answers
        self.question.always_requires_review = False
        self.question.review_answer_value = "high"
        self.question.operator = "equals"
        self.question.save()

        # Create answer that should trigger review
        answer = factories.AnswerFactory(
            question=self.question, user=self.fixture.user, answer_data="high"
        )

        self.assertTrue(answer.requires_review)

    def test_answer_not_flagged_when_condition_not_met(self):
        """Test that answers are not flagged when trigger conditions aren't met."""
        # Configure question to trigger review for "high" answers
        self.question.always_requires_review = False
        self.question.review_answer_value = "high"
        self.question.operator = "equals"
        self.question.save()

        # Create answer that should not trigger review
        answer = factories.AnswerFactory(
            question=self.question, user=self.fixture.user, answer_data="low"
        )

        self.assertFalse(answer.requires_review)

    def test_answer_always_flagged_when_always_requires_review(self):
        """Test that answers are always flagged when always_requires_review is True."""
        self.question.always_requires_review = True
        self.question.save()

        answer = factories.AnswerFactory(
            question=self.question, user=self.fixture.user, answer_data="any value"
        )

        self.assertTrue(answer.requires_review)
