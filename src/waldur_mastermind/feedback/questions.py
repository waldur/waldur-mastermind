"""A feedback survey's questions as one nested payload.

The client names questions and options by keys of its own choosing, and a
condition refers to them by those keys, so a whole question set can be
validated and created in one request. Keys are opaque: they only tie the
payload together, and every row created from it is new.
"""

import uuid
from collections import Counter
from typing import NoReturn

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.checklist import utils as checklist_utils
from waldur_core.core.models import DESCRIPTION_LENGTH
from waldur_core.core.openapi_extensions import AnyJSONField

QuestionTypes = checklist_enums.QuestionTypes

SURVEY_QUESTION_TYPES = [
    (value, label)
    for value, label in QuestionTypes.CHOICES
    if value not in (QuestionTypes.FILE, QuestionTypes.MULTIPLE_FILES)
]

_NESTED_FIELDS = ("key", "options", "conditions")


class SurveyQuestionOptionSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField(max_length=255)


class SurveyQuestionConditionSerializer(serializers.Serializer):
    depends_on = serializers.CharField(help_text="Key of the question it depends on.")
    operator = serializers.ChoiceField(choices=checklist_enums.OPERATORS)
    required_answer_value = AnyJSONField(
        help_text="For a select question, option keys instead of option UUIDs."
    )


class SurveyQuestionSerializer(serializers.Serializer):
    key = serializers.CharField()
    description = serializers.CharField(max_length=DESCRIPTION_LENGTH)
    question_type = serializers.ChoiceField(choices=SURVEY_QUESTION_TYPES)
    required = serializers.BooleanField(default=False)
    user_guidance = serializers.CharField(default="", allow_blank=True)
    min_value = serializers.DecimalField(
        max_digits=20, decimal_places=4, required=False, allow_null=True
    )
    max_value = serializers.DecimalField(
        max_digits=20, decimal_places=4, required=False, allow_null=True
    )
    likert_scale_length = serializers.ChoiceField(
        choices=checklist_enums.LikertScaleLengths.CHOICES,
        required=False,
        allow_null=True,
    )
    likert_low_label = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )
    likert_high_label = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )
    likert_allow_na = serializers.BooleanField(required=False)
    rich_text_char_limit = serializers.IntegerField(
        min_value=1, required=False, allow_null=True
    )
    rich_text_toolbar_level = serializers.ChoiceField(
        choices=checklist_enums.RichTextToolbarLevels.CHOICES, required=False
    )
    dependency_logic_operator = serializers.ChoiceField(
        choices=checklist_enums.DependencyLogicOperators.CHOICES, required=False
    )
    options = SurveyQuestionOptionSerializer(many=True, default=list)
    conditions = SurveyQuestionConditionSerializer(many=True, default=list)


def _refuse(message) -> NoReturn:
    raise serializers.ValidationError({"questions": [message]})


def _question_fields(values):
    return {name: value for name, value in values.items() if name not in _NESTED_FIELDS}


def _validate_question(values):
    name = values["description"]
    # The same rules as a question created through the checklist admin API.
    try:
        checklist_serializers.QuestionAdminSerializer().validate(
            _question_fields(values)
        )
    except serializers.ValidationError as e:
        _refuse(f"Question '{name}': {' '.join(map(str, e.detail))}")

    options = values["options"]
    if options and values["question_type"] not in checklist_utils.SELECT_QUESTION_TYPES:
        _refuse(f"Question '{name}' is not a select question, so it has no options.")
    keys = Counter(option["key"] for option in options)
    for key, count in keys.items():
        if count > 1:
            _refuse(f"Question '{name}' has option key '{key}' more than once.")


def _validate_condition(values, condition, by_key):
    name = values["description"]
    depends_on = condition["depends_on"]
    if depends_on == values["key"]:
        _refuse(f"Question '{name}' cannot depend on itself.")
    parent = by_key.get(depends_on)
    if parent is None:
        _refuse(f"Question '{name}' depends on unknown question '{depends_on}'.")

    parent_type = parent["question_type"]
    operator = condition["operator"]
    if not checklist_utils.is_valid_operator_for_question_type(parent_type, operator):
        _refuse(
            f"Operator '{operator}' is not valid for question "
            f"'{parent['description']}' (type: {parent_type})."
        )

    value = condition["required_answer_value"]
    if parent_type in checklist_utils.SELECT_QUESTION_TYPES:
        # Stand-ins for the UUIDs the options get on create, so that the
        # checklist rules on how many options a condition holds still apply.
        placeholders = dict.fromkeys(
            (option["key"] for option in parent["options"]), uuid.UUID(int=0).hex
        )
        try:
            value = checklist_utils.map_option_references(value, placeholders)
        except KeyError as e:
            _refuse(f"Question '{parent['description']}' has no option {e.args[0]!r}.")
    if not checklist_utils.is_valid_condition_value(value, parent_type):
        _refuse(
            f"Required answer value {condition['required_answer_value']!r} is not "
            f"valid for question '{parent['description']}' (type: {parent_type})."
        )


def _validate_acyclic(questions, by_key):
    visiting, done = set(), set()

    def visit(key, path):
        if key in done:
            return
        if key in visiting:
            cycle = path[path.index(key) :] + [key]
            _refuse(
                "Question conditions form a cycle: "
                + " -> ".join(f"'{by_key[k]['description']}'" for k in cycle)
                + "."
            )
        visiting.add(key)
        for condition in by_key[key]["conditions"]:
            visit(condition["depends_on"], path + [key])
        visiting.discard(key)
        done.add(key)

    for values in questions:
        visit(values["key"], [])


def validate_question_set(questions: list[dict]) -> None:
    """Refuse a question set that ``create_question_set`` could not create
    faithfully. It writes nothing, so the caller can run it before any row."""
    for key, count in Counter(values["key"] for values in questions).items():
        if count > 1:
            _refuse(f"Question key '{key}' is used more than once.")

    # Respondents' browsers find the question a condition depends on by its
    # text, so two questions with one text would bind conditions wrongly.
    descriptions = Counter(values["description"].strip() for values in questions)
    for description, count in descriptions.items():
        if count > 1:
            _refuse(f"Question '{description}' is listed more than once.")

    by_key = {values["key"]: values for values in questions}
    for values in questions:
        _validate_question(values)
        for condition in values["conditions"]:
            _validate_condition(values, condition, by_key)

    _validate_acyclic(questions, by_key)


def create_question_set(
    checklist: checklist_models.Checklist, questions: list[dict]
) -> None:
    """Create a validated question set in ``checklist``, ordered as listed.

    The caller owns the transaction."""
    created = {}
    for order, values in enumerate(questions):
        question = checklist_models.Question.objects.create(
            checklist=checklist, order=order, **_question_fields(values)
        )
        option_uuids = {}
        for option_order, option in enumerate(values["options"]):
            option_uuids[option["key"]] = (
                checklist_models.QuestionOption.objects.create(
                    question=question, label=option["label"], order=option_order
                ).uuid.hex
            )
        created[values["key"]] = question, option_uuids

    for values in questions:
        question, _ = created[values["key"]]
        for condition in values["conditions"]:
            parent, option_uuids = created[condition["depends_on"]]
            value = condition["required_answer_value"]
            if parent.question_type in checklist_utils.SELECT_QUESTION_TYPES:
                value = checklist_utils.map_option_references(value, option_uuids)
            checklist_models.QuestionDependency.objects.create(
                question=question,
                depends_on_question=parent,
                operator=condition["operator"],
                required_answer_value=value,
            )


def replace_question_set(
    checklist: checklist_models.Checklist, questions: list[dict]
) -> None:
    """Replace every question of ``checklist`` with a validated question set.

    It deletes answers given to the old questions, so the caller makes sure
    there are none, and owns the transaction."""
    checklist.questions.all().delete()
    create_question_set(checklist, questions)


# Fields of a question that only its type uses. Every question row holds them
# with a default, which the type rules refuse on any other type.
_TYPE_ONLY_FIELDS = {
    QuestionTypes.NUMBER: ("min_value", "max_value"),
    QuestionTypes.YEAR: ("min_value", "max_value"),
    QuestionTypes.RATING: ("min_value", "max_value"),
    QuestionTypes.LIKERT: (
        "likert_scale_length",
        "likert_low_label",
        "likert_high_label",
        "likert_allow_na",
    ),
    QuestionTypes.RICH_TEXT: ("rich_text_char_limit", "rich_text_toolbar_level"),
}


def _dump_condition(dependency, questions_by_id):
    # The checklist admin lets a condition point into another checklist; such
    # a parent is dumped as an unknown key, which sending the set back names.
    parent = questions_by_id.get(
        dependency.depends_on_question_id, dependency.depends_on_question
    )
    value = dependency.required_answer_value
    if parent.question_type in checklist_utils.SELECT_QUESTION_TYPES:
        # Rows written through the checklist admin may hold dashed uuids.
        references = {}
        for option in parent.question_options.all():
            references[option.uuid.hex] = option.uuid.hex
            references[str(uuid.UUID(option.uuid.hex))] = option.uuid.hex
        try:
            value = checklist_utils.map_option_references(value, references)
        except KeyError:
            # A dangling reference is sent as it is, so that sending the set
            # back names the missing option instead of losing the condition.
            pass
    return {
        "depends_on": parent.uuid.hex,
        "operator": dependency.operator,
        "required_answer_value": value,
    }


def dump_question_set(checklist: checklist_models.Checklist) -> list[dict]:
    """The questions of ``checklist`` in the shape ``create_question_set``
    takes, keyed by uuid hex, so that a read can be sent back as it is."""
    question_list = list(checklist.get_questions())
    questions_by_id = {question.pk: question for question in question_list}
    dumped = []
    for question in question_list:
        values = {
            "key": question.uuid.hex,
            "description": question.description,
            "question_type": question.question_type,
            "required": question.required,
            "user_guidance": question.user_guidance,
            "dependency_logic_operator": question.dependency_logic_operator,
            "options": [
                {"key": option.uuid.hex, "label": option.label}
                for option in question.question_options.all()
            ]
            if question.question_type in checklist_utils.SELECT_QUESTION_TYPES
            else [],
            "conditions": [
                _dump_condition(dependency, questions_by_id)
                for dependency in question.dependencies.all()
            ],
        }
        for name in _TYPE_ONLY_FIELDS.get(question.question_type, ()):
            values[name] = getattr(question, name)
        dumped.append(values)
    return dumped


def _question_set_serializer(**kwargs):
    return SurveyQuestionSerializer(
        many=True, allow_empty=False, max_length=100, **kwargs
    )


@extend_schema_field(_question_set_serializer())
class SurveyQuestionSetField(serializers.Field):
    """A survey's question set: read from its checklist, written whole.

    The set is validated on its own because a nested serializer under a
    partial update skips its items' required fields and defaults."""

    def get_attribute(self, instance):
        return instance.checklist

    def to_representation(self, value):
        return _question_set_serializer(instance=dump_question_set(value)).data

    def to_internal_value(self, data):
        serializer = _question_set_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data
