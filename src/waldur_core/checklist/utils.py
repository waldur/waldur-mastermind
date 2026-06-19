import datetime

from waldur_core.core import utils as core_utils
from waldur_mastermind.common.utils import parse_date

from . import enums


def is_valid_operator_for_question_type(question_type, operator):
    """Validates if a comparison operator is compatible with a specific question type."""
    valid_operators = {
        "equals": [
            enums.QuestionTypes.NUMBER,
            enums.QuestionTypes.DATE,
            enums.QuestionTypes.BOOLEAN,
            enums.QuestionTypes.FILE,
            enums.QuestionTypes.YEAR,
            enums.QuestionTypes.PHONE_NUMBER,
            enums.QuestionTypes.EMAIL,
            enums.QuestionTypes.URL,
            enums.QuestionTypes.COUNTRY,
            enums.QuestionTypes.RATING,
            enums.QuestionTypes.DATETIME,
            enums.QuestionTypes.LIKERT,
        ],
        "not_equals": [
            enums.QuestionTypes.NUMBER,
            enums.QuestionTypes.DATE,
            enums.QuestionTypes.BOOLEAN,
            enums.QuestionTypes.FILE,
            enums.QuestionTypes.YEAR,
            enums.QuestionTypes.PHONE_NUMBER,
            enums.QuestionTypes.EMAIL,
            enums.QuestionTypes.URL,
            enums.QuestionTypes.COUNTRY,
            enums.QuestionTypes.RATING,
            enums.QuestionTypes.DATETIME,
            enums.QuestionTypes.LIKERT,
        ],
        "contains": [
            enums.QuestionTypes.TEXT_INPUT,
            enums.QuestionTypes.TEXT_AREA,
            enums.QuestionTypes.FILE,
            enums.QuestionTypes.MULTIPLE_FILES,
            enums.QuestionTypes.PHONE_NUMBER,
            enums.QuestionTypes.EMAIL,
            enums.QuestionTypes.URL,
        ],
        "in": [
            enums.QuestionTypes.MULTI_SELECT,
            enums.QuestionTypes.SINGLE_SELECT,
            enums.QuestionTypes.MULTIPLE_FILES,
            enums.QuestionTypes.COUNTRY,
        ],
        "not_in": [
            enums.QuestionTypes.MULTI_SELECT,
            enums.QuestionTypes.SINGLE_SELECT,
            enums.QuestionTypes.MULTIPLE_FILES,
            enums.QuestionTypes.COUNTRY,
        ],
    }
    if question_type in valid_operators[operator]:
        return True

    return False


def _is_valid_trigger_value(
    answer_data: list[str] | str | int | float | bool | datetime.date,
    question_type: str,
) -> bool:
    """Internal validator that checks if answer data matches expected format for the question type."""
    if (
        isinstance(answer_data, list)
        and len(answer_data) == 1
        and all(
            isinstance(item, str) and core_utils.is_uuid_like(item)
            for item in answer_data
        )
        and question_type
        in [
            enums.QuestionTypes.SINGLE_SELECT,
        ]
    ):
        return True

    if (
        isinstance(answer_data, list)
        and all(
            isinstance(item, str) and core_utils.is_uuid_like(item)
            for item in answer_data
        )
        and question_type
        in [
            enums.QuestionTypes.MULTI_SELECT,
        ]
    ):
        return True

    if isinstance(answer_data, datetime.date) and question_type in [
        enums.QuestionTypes.DATE,
    ]:
        return True

    # Allow both numeric types and string representations of numbers for NUMBER type
    if question_type == enums.QuestionTypes.NUMBER:
        if isinstance(answer_data, int | float):
            return True
        # Also accept string representations of numbers
        if isinstance(answer_data, str):
            try:
                float(answer_data)
                return True
            except (ValueError, TypeError):
                return False

    # YEAR and RATING are integer types
    if question_type in [enums.QuestionTypes.YEAR, enums.QuestionTypes.RATING]:
        if isinstance(answer_data, int):
            return True
        # Also accept string representations of integers
        if isinstance(answer_data, str):
            try:
                int(answer_data)
                return True
            except (ValueError, TypeError):
                return False

    if isinstance(answer_data, bool | type(None)) and question_type in [
        enums.QuestionTypes.BOOLEAN,
    ]:
        return True

    # File types validation
    if isinstance(answer_data, dict) and question_type in [
        enums.QuestionTypes.FILE,
    ]:
        return True

    if isinstance(answer_data, list) and question_type in [
        enums.QuestionTypes.MULTIPLE_FILES,
    ]:
        return True

    # LIKERT answers are integers (scale position, 0-based) or the literal "na"
    if question_type == enums.QuestionTypes.LIKERT:
        if answer_data == "na":
            return True
        if isinstance(answer_data, bool):
            return False
        if isinstance(answer_data, int):
            return True
        if isinstance(answer_data, str):
            try:
                int(answer_data)
                return True
            except (ValueError, TypeError):
                return False

    # RICH_TEXT answers are arbitrary strings
    if question_type == enums.QuestionTypes.RICH_TEXT and isinstance(answer_data, str):
        return True

    return False


def is_valid_condition_value(
    answer_data: list[str] | str | int | float | bool | datetime.date,
    question_type: str,
) -> bool:
    """Validates values used in question dependencies and conditions, allowing text lists for text inputs."""
    if (
        isinstance(answer_data, list)
        and question_type
        in [
            enums.QuestionTypes.TEXT_INPUT,
            enums.QuestionTypes.TEXT_AREA,
            enums.QuestionTypes.PHONE_NUMBER,
            enums.QuestionTypes.EMAIL,
            enums.QuestionTypes.URL,
            enums.QuestionTypes.COUNTRY,  # Allow list of countries for 'in'/'not_in' operators
        ]
    ):
        return True

    # Handle date strings for DATE type questions
    if isinstance(answer_data, str) and question_type == enums.QuestionTypes.DATE:
        try:
            # Try to parse the date string using the utility function
            parse_date(answer_data)
            return True
        except (ValueError, TypeError):
            return False

    # Handle datetime strings for DATETIME type questions
    if isinstance(answer_data, str) and question_type == enums.QuestionTypes.DATETIME:
        try:
            datetime.datetime.fromisoformat(answer_data)
            return True
        except (ValueError, TypeError):
            return False

    # Handle string values for string-based types in conditions
    if isinstance(answer_data, str) and question_type in [
        enums.QuestionTypes.PHONE_NUMBER,
        enums.QuestionTypes.EMAIL,
        enums.QuestionTypes.URL,
        enums.QuestionTypes.COUNTRY,
    ]:
        return True

    return _is_valid_trigger_value(answer_data, question_type)


def is_valid_answer(
    answer_data: list[str] | str | int | float | bool | datetime.date,
    question_type: str,
) -> bool:
    """Validates user-submitted answers, ensuring strings for text inputs and proper formats for other types."""
    if isinstance(answer_data, str) and question_type in [
        enums.QuestionTypes.TEXT_INPUT,
        enums.QuestionTypes.TEXT_AREA,
        enums.QuestionTypes.PHONE_NUMBER,
        enums.QuestionTypes.EMAIL,
        enums.QuestionTypes.URL,
        enums.QuestionTypes.COUNTRY,
    ]:
        return True

    # Handle date strings for DATE type questions
    if isinstance(answer_data, str) and question_type == enums.QuestionTypes.DATE:
        try:
            # Try to parse the date string using the utility function
            parse_date(answer_data)
            return True
        except (ValueError, TypeError):
            return False

    # Handle datetime strings for DATETIME type questions
    if isinstance(answer_data, str) and question_type == enums.QuestionTypes.DATETIME:
        try:
            # Try to parse the datetime string
            datetime.datetime.fromisoformat(answer_data)
            return True
        except (ValueError, TypeError):
            return False

    # Basic file type validation (detailed validation is done in Question.is_valid_file_answer)
    if question_type == enums.QuestionTypes.FILE:
        return isinstance(answer_data, dict)

    if question_type == enums.QuestionTypes.MULTIPLE_FILES:
        return isinstance(answer_data, list)

    return _is_valid_trigger_value(answer_data, question_type)


def apply_operator(user_answer: any, required_value: any, operator: str) -> bool:
    """Core comparison engine that applies operators between user answers and required values for dependency evaluation and review triggering."""
    if user_answer is None:
        return False

    # Handle date comparison - convert string dates to date objects for comparison
    if isinstance(required_value, str) and isinstance(user_answer, datetime.date):
        try:
            required_value = parse_date(required_value)
        except (ValueError, TypeError):
            return False
    elif isinstance(user_answer, str) and isinstance(required_value, datetime.date):
        try:
            user_answer = parse_date(user_answer)
        except (ValueError, TypeError):
            return False

    if operator == "equals":
        return user_answer == required_value
    elif operator == "not_equals":
        return user_answer != required_value
    elif operator == "contains":
        return any(substr in user_answer for substr in required_value)
    elif operator == "in":
        if isinstance(required_value, list):
            return any([a in required_value for a in user_answer])
        return user_answer == required_value
    elif operator == "not_in":
        if isinstance(required_value, list):
            return not any([a in required_value for a in user_answer])
        return user_answer != required_value

    return False


def serialize_completion_answers(completion) -> list[dict]:
    """Render a checklist completion's answers as a list of read-only dicts.

    Each entry is ``{question_uuid, question, question_type, answer}`` ordered by
    question order. ``answer`` is the human-readable value: for select-type questions
    the stored option UUIDs are resolved to labels via ``Question.get_answer_display``.

    Pass a completion whose ``answers__question__question_options`` are prefetched to
    avoid N+1 queries.
    """
    answers = sorted(completion.answers.all(), key=lambda answer: answer.question.order)
    return [
        {
            "question_uuid": answer.question.uuid.hex,
            "question": answer.question.description,
            "question_type": answer.question.question_type,
            "answer": answer.question.get_answer_display(answer.answer_data),
        }
        for answer in answers
    ]
