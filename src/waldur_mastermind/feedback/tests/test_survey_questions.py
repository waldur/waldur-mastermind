import uuid

from ddt import data, ddt
from django.test import TestCase
from rest_framework import serializers

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_mastermind.feedback import questions

QuestionTypes = checklist_enums.QuestionTypes


class _SurveySerializer(serializers.Serializer):
    """Stands in for the survey serializer that owns the question set."""

    questions = questions.SurveyQuestionSerializer(many=True)

    def validate(self, attrs):
        questions.validate_question_set(attrs["questions"])
        return attrs


def _question(key, question_type=QuestionTypes.TEXT_INPUT, **fields):
    return {
        "key": key,
        "description": fields.pop("description", f"Question {key}"),
        "question_type": question_type,
        **fields,
    }


def _select(key, question_type=QuestionTypes.SINGLE_SELECT, **fields):
    return _question(
        key,
        question_type,
        options=[{"key": "opt-a", "label": "A"}, {"key": "opt-b", "label": "B"}],
        **fields,
    )


def _condition(depends_on, required_answer_value, operator="in"):
    return {
        "depends_on": depends_on,
        "operator": operator,
        "required_answer_value": required_answer_value,
    }


# The fields a question of each type needs before the checklist rules accept it.
_TYPE_FIELDS = {
    QuestionTypes.SINGLE_SELECT: {
        "options": [{"key": "a", "label": "First"}, {"key": "b", "label": "Second"}]
    },
    QuestionTypes.MULTI_SELECT: {
        "options": [{"key": "a", "label": "First"}, {"key": "b", "label": "Second"}]
    },
    QuestionTypes.RATING: {"min_value": 1, "max_value": 5},
    QuestionTypes.LIKERT: {"likert_scale_length": 5},
}


@ddt
class SurveyQuestionSetTest(TestCase):
    def setUp(self):
        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.FEEDBACK
        )

    def validate(self, payload):
        serializer = _SurveySerializer(data={"questions": payload})
        serializer.is_valid()
        return serializer

    def create(self, payload):
        serializer = self.validate(payload)
        self.assertEqual(serializer.errors, {})
        questions.create_question_set(
            self.checklist, serializer.validated_data["questions"]
        )
        return {q.description: q for q in self.checklist.questions.all()}

    def dependency(self, question):
        return checklist_models.QuestionDependency.objects.get(question=question)

    def test_creates_every_question_type_with_order(self):
        types = [value for value, _ in questions.SURVEY_QUESTION_TYPES]
        self.create(
            [
                _question(
                    str(index), question_type, **_TYPE_FIELDS.get(question_type, {})
                )
                for index, question_type in enumerate(types)
            ]
        )

        created = list(self.checklist.questions.order_by("order"))
        self.assertEqual([q.order for q in created], list(range(len(types))))
        self.assertEqual([q.question_type for q in created], types)
        for question in created:
            if (
                question.question_type in _TYPE_FIELDS
                and "options" in _TYPE_FIELDS[question.question_type]
            ):
                self.assertEqual(
                    [(o.label, o.order) for o in question.question_options.all()],
                    [("First", 0), ("Second", 1)],
                )
            else:
                self.assertFalse(question.question_options.exists())

    def test_file_types_are_not_offered(self):
        types = {value for value, _ in questions.SURVEY_QUESTION_TYPES}
        self.assertNotIn(QuestionTypes.FILE, types)
        self.assertNotIn(QuestionTypes.MULTIPLE_FILES, types)
        self.assertEqual(
            types | {QuestionTypes.FILE, QuestionTypes.MULTIPLE_FILES},
            {value for value, _ in QuestionTypes.CHOICES},
        )

    def test_dump_of_every_type_validates_when_sent_back(self):
        types = [value for value, _ in questions.SURVEY_QUESTION_TYPES]
        self.create(
            [
                _question(
                    str(index), question_type, **_TYPE_FIELDS.get(question_type, {})
                )
                for index, question_type in enumerate(types)
            ]
        )

        dumped = questions.dump_question_set(self.checklist)

        self.assertEqual([q["question_type"] for q in dumped], types)
        self.assertEqual(self.validate(dumped).errors, {})
        text = dumped[types.index(QuestionTypes.TEXT_INPUT)]
        self.assertNotIn("rich_text_toolbar_level", text)
        self.assertNotIn("likert_allow_na", text)
        self.assertNotIn("min_value", text)

    def test_replace_question_set_drops_the_old_questions(self):
        old = self.create([_select("parent", description="Parent")])["Parent"]
        serializer = self.validate([_question("new", description="New")])

        questions.replace_question_set(
            self.checklist, serializer.validated_data["questions"]
        )

        self.assertEqual(
            [q.description for q in self.checklist.questions.all()], ["New"]
        )
        self.assertFalse(
            checklist_models.QuestionOption.objects.filter(question=old).exists()
        )

    def test_creates_question_fields(self):
        created = self.create(
            [
                _question(
                    "q",
                    QuestionTypes.LIKERT,
                    description="How was it?",
                    required=True,
                    user_guidance="Be honest",
                    likert_scale_length=7,
                    likert_low_label="Bad",
                    likert_high_label="Good",
                    likert_allow_na=True,
                )
            ]
        )
        question = created["How was it?"]
        self.assertTrue(question.required)
        self.assertEqual(question.user_guidance, "Be honest")
        self.assertEqual(question.likert_scale_length, 7)
        self.assertEqual(question.likert_low_label, "Bad")
        self.assertEqual(question.likert_high_label, "Good")
        self.assertTrue(question.likert_allow_na)

    def test_condition_on_single_select_maps_option_key(self):
        created = self.create(
            [
                _select("parent", description="Parent"),
                _question(
                    "child",
                    description="Child",
                    conditions=[_condition("parent", ["opt-a"])],
                ),
            ]
        )
        option_a = created["Parent"].question_options.get(label="A")

        dependency = self.dependency(created["Child"])
        self.assertEqual(dependency.depends_on_question, created["Parent"])
        self.assertEqual(dependency.operator, "in")
        self.assertEqual(dependency.required_answer_value, [option_a.uuid.hex])

    def test_condition_on_multi_select_maps_every_option_key(self):
        created = self.create(
            [
                _select("parent", QuestionTypes.MULTI_SELECT, description="Parent"),
                _question(
                    "child",
                    description="Child",
                    conditions=[_condition("parent", ["opt-a", "opt-b"])],
                ),
            ]
        )
        options = created["Parent"].question_options.all()

        self.assertEqual(
            self.dependency(created["Child"]).required_answer_value,
            [option.uuid.hex for option in options],
        )

    def test_condition_on_rating_keeps_value(self):
        created = self.create(
            [
                _question(
                    "parent",
                    QuestionTypes.RATING,
                    description="Parent",
                    min_value=1,
                    max_value=5,
                ),
                _question(
                    "child",
                    description="Child",
                    conditions=[_condition("parent", 4, operator="equals")],
                ),
            ]
        )

        self.assertEqual(self.dependency(created["Child"]).required_answer_value, 4)

    def test_accepts_a_question_reached_by_two_paths(self):
        created = self.create(
            [
                _question("a", QuestionTypes.BOOLEAN),
                _question("b", conditions=[_condition("a", True, operator="equals")]),
                _question("c", conditions=[_condition("a", True, operator="equals")]),
                _question(
                    "d",
                    conditions=[
                        _condition("b", ["x"], operator="contains"),
                        _condition("c", ["y"], operator="contains"),
                    ],
                ),
            ]
        )

        self.assertEqual(created["Question d"].dependencies.count(), 2)

    def test_accepts_dashed_and_hex_uuid_keys(self):
        parent_key = uuid.uuid4().hex
        child_key = str(uuid.uuid4())
        option_key = str(uuid.uuid4())
        created = self.create(
            [
                _question(
                    parent_key,
                    QuestionTypes.MULTI_SELECT,
                    description="Parent",
                    options=[
                        {"key": option_key, "label": "A"},
                        {"key": uuid.uuid4().hex, "label": "B"},
                    ],
                ),
                _question(
                    child_key,
                    description="Child",
                    conditions=[_condition(parent_key, [option_key])],
                ),
            ]
        )
        option_a = created["Parent"].question_options.get(label="A")

        value = self.dependency(created["Child"]).required_answer_value
        self.assertEqual(value, [option_a.uuid.hex])
        self.assertNotEqual(option_a.uuid.hex, uuid.UUID(option_key).hex)

    @data(
        (
            "duplicate key",
            [_question("q", description="One"), _question("q", description="Two")],
        ),
        (
            "duplicate description",
            [_question("a", description=" Q1"), _question("b", description="Q1")],
        ),
        (
            "unknown depends_on",
            [_question("a", conditions=[_condition("missing", ["x"], "contains")])],
        ),
        (
            "self-dependency",
            [_question("a", conditions=[_condition("a", ["x"], "contains")])],
        ),
        (
            "two-cycle",
            [
                _question("a", conditions=[_condition("b", ["x"], "contains")]),
                _question("b", conditions=[_condition("a", ["x"], "contains")]),
            ],
        ),
        (
            "unknown option key",
            [
                _select("parent"),
                _question("child", conditions=[_condition("parent", ["opt-c"])]),
            ],
        ),
        (
            "single select condition as a scalar, not a one-item list",
            [
                _select("parent"),
                _question("child", conditions=[_condition("parent", "opt-a")]),
            ],
        ),
        (
            "unknown uuid option key",
            [
                _select("parent"),
                _question(
                    "child", conditions=[_condition("parent", [uuid.uuid4().hex])]
                ),
            ],
        ),
        (
            "single select condition holding two options",
            [
                _select("parent"),
                _question(
                    "child", conditions=[_condition("parent", ["opt-a", "opt-b"])]
                ),
            ],
        ),
        (
            "operator invalid for the parent type",
            [
                _select("parent"),
                _question(
                    "child",
                    conditions=[_condition("parent", ["opt-a"], operator="equals")],
                ),
            ],
        ),
        (
            "value invalid for the parent type",
            [
                _question("parent", QuestionTypes.RATING, min_value=1, max_value=5),
                _question(
                    "child",
                    conditions=[_condition("parent", "many", operator="equals")],
                ),
            ],
        ),
        ("file question", [_question("a", QuestionTypes.FILE)]),
        ("multiple files question", [_question("a", QuestionTypes.MULTIPLE_FILES)]),
        (
            "duplicate option key",
            [
                _question(
                    "a",
                    QuestionTypes.SINGLE_SELECT,
                    options=[{"key": "x", "label": "A"}, {"key": "x", "label": "B"}],
                )
            ],
        ),
        (
            "options on a question that is not a select",
            [_question("a", options=[{"key": "x", "label": "A"}])],
        ),
        ("likert without a scale length", [_question("a", QuestionTypes.LIKERT)]),
        (
            "min value above max value",
            [_question("a", QuestionTypes.RATING, min_value=5, max_value=1)],
        ),
    )
    def test_refuses(self, case):
        _, payload = case
        serializer = self.validate(payload)

        self.assertIn("questions", serializer.errors)
