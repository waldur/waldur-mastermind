"""Tests for LIKERT and RICH_TEXT question types."""

from rest_framework import status, test

from waldur_core.checklist import enums, models, utils
from waldur_core.checklist.tests import factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class LikertAnswerValidationTest(test.APITestCase):
    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def _make_question(self, **kwargs):
        kwargs.setdefault("likert_scale_length", enums.LikertScaleLengths.FIVE)
        return factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.LIKERT,
            operator="",
            **kwargs,
        )

    def test_integer_in_range_is_valid(self):
        question = self._make_question()
        for value in range(5):
            with self.subTest(value=value):
                self.assertTrue(question.is_valid_answer(value))

    def test_integer_out_of_range_is_rejected(self):
        question = self._make_question()
        self.assertFalse(question.is_valid_answer(-1))
        self.assertFalse(question.is_valid_answer(5))
        self.assertFalse(question.is_valid_answer(99))

    def test_seven_point_scale_accepts_six(self):
        question = self._make_question(
            likert_scale_length=enums.LikertScaleLengths.SEVEN
        )
        self.assertTrue(question.is_valid_answer(6))
        self.assertFalse(question.is_valid_answer(7))

    def test_na_requires_allow_na_flag(self):
        question = self._make_question(likert_allow_na=False)
        self.assertFalse(question.is_valid_answer("na"))

        question_with_na = self._make_question(likert_allow_na=True)
        self.assertTrue(question_with_na.is_valid_answer("na"))

    def test_boolean_is_rejected(self):
        question = self._make_question()
        # bool is a subclass of int in Python — we must explicitly reject it
        self.assertFalse(question.is_valid_answer(True))
        self.assertFalse(question.is_valid_answer(False))

    def test_arbitrary_string_is_rejected(self):
        question = self._make_question(likert_allow_na=True)
        self.assertFalse(question.is_valid_answer("agree"))


class RichTextAnswerValidationTest(test.APITestCase):
    def setUp(self):
        self.checklist = factories.ChecklistFactory()

    def _make_question(self, **kwargs):
        return factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.RICH_TEXT,
            operator="",
            **kwargs,
        )

    def test_string_answer_is_valid(self):
        question = self._make_question()
        self.assertTrue(question.is_valid_answer("# Heading\n\nSome **markdown**."))

    def test_non_string_is_rejected(self):
        question = self._make_question()
        self.assertFalse(question.is_valid_answer(123))
        self.assertFalse(question.is_valid_answer({"text": "x"}))

    def test_char_limit_is_enforced(self):
        question = self._make_question(rich_text_char_limit=10)
        self.assertTrue(question.is_valid_answer("short"))
        self.assertTrue(question.is_valid_answer("exactly10!"))
        self.assertFalse(question.is_valid_answer("definitely too long"))

    def test_no_limit_allows_any_length(self):
        question = self._make_question()
        self.assertTrue(question.is_valid_answer("x" * 100_000))


class LikertOperatorTest(test.APITestCase):
    def test_likert_supports_equals_and_not_equals(self):
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.LIKERT, "equals"
            )
        )
        self.assertTrue(
            utils.is_valid_operator_for_question_type(
                enums.QuestionTypes.LIKERT, "not_equals"
            )
        )

    def test_likert_does_not_support_contains_or_in(self):
        for op in ("contains", "in", "not_in"):
            with self.subTest(op=op):
                self.assertFalse(
                    utils.is_valid_operator_for_question_type(
                        enums.QuestionTypes.LIKERT, op
                    )
                )

    def test_likert_condition_value_accepts_int_and_na(self):
        self.assertTrue(utils.is_valid_condition_value(3, enums.QuestionTypes.LIKERT))
        self.assertTrue(
            utils.is_valid_condition_value("na", enums.QuestionTypes.LIKERT)
        )

    def test_likert_condition_value_rejects_arbitrary_string(self):
        self.assertFalse(
            utils.is_valid_condition_value("agree", enums.QuestionTypes.LIKERT)
        )


class QuestionAdminLikertCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.checklist = factories.ChecklistFactory()
        self.url = factories.QuestionFactory.get_admin_list_url()

    def _payload(self, **overrides):
        payload = {
            "description": "How satisfied are you?",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.LIKERT,
            "likert_scale_length": enums.LikertScaleLengths.FIVE,
            "likert_low_label": "Very unsatisfied",
            "likert_high_label": "Very satisfied",
            "likert_allow_na": True,
        }
        payload.update(overrides)
        return payload

    def test_create_likert_question(self):
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        question = models.Question.objects.get(uuid=response.data["uuid"])
        self.assertEqual(question.likert_scale_length, 5)
        self.assertEqual(question.likert_low_label, "Very unsatisfied")
        self.assertTrue(question.likert_allow_na)

    def test_likert_scale_length_required_for_likert_type(self):
        response = self.client.post(
            self.url, self._payload(likert_scale_length=None), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("likert_scale_length", str(response.data))

    def test_likert_fields_rejected_on_non_likert_question(self):
        payload = {
            "description": "What is your name?",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "likert_scale_length": 5,
        }
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Likert", str(response.data))

    def test_invalid_likert_scale_length_rejected(self):
        response = self.client.post(
            self.url, self._payload(likert_scale_length=4), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_cannot_set_likert_field_on_non_likert_question(self):
        text_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.TEXT_INPUT,
        )
        response = self.client.patch(
            factories.QuestionFactory.get_admin_url(text_question),
            {"likert_scale_length": 5},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Likert", str(response.data))


class QuestionAdminRichTextCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.checklist = factories.ChecklistFactory()
        self.url = factories.QuestionFactory.get_admin_list_url()

    def _payload(self, **overrides):
        payload = {
            "description": "Describe your project",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.RICH_TEXT,
            "rich_text_char_limit": 5000,
            "rich_text_toolbar_level": enums.RichTextToolbarLevels.EXTENDED,
        }
        payload.update(overrides)
        return payload

    def test_create_rich_text_question(self):
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        question = models.Question.objects.get(uuid=response.data["uuid"])
        self.assertEqual(question.rich_text_char_limit, 5000)
        self.assertEqual(question.rich_text_toolbar_level, "extended")

    def test_default_toolbar_level_is_standard(self):
        payload = self._payload()
        payload.pop("rich_text_toolbar_level")
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        question = models.Question.objects.get(uuid=response.data["uuid"])
        self.assertEqual(question.rich_text_toolbar_level, "standard")

    def test_rich_text_fields_rejected_on_non_rich_text_question(self):
        payload = {
            "description": "What is your name?",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.TEXT_INPUT,
            "rich_text_char_limit": 100,
        }
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Rich text", str(response.data))

    def test_negative_char_limit_rejected(self):
        response = self.client.post(
            self.url, self._payload(rich_text_char_limit=0), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_cannot_set_rich_text_field_on_non_rich_text_question(self):
        text_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.TEXT_INPUT,
        )
        response = self.client.patch(
            factories.QuestionFactory.get_admin_url(text_question),
            {"rich_text_char_limit": 100},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Rich text", str(response.data))
