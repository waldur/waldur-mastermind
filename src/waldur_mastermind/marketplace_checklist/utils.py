import datetime

from waldur_core.core import utils as core_utils

from . import enums


def is_valid_operator_for_question_type(question_type, operator):
    valid_operators = {
        "equals": [
            enums.QuestionTypes.NUMBER,
            enums.QuestionTypes.DATE,
            enums.QuestionTypes.BOOLEAN,
        ],
        "not_equals": [
            enums.QuestionTypes.NUMBER,
            enums.QuestionTypes.DATE,
            enums.QuestionTypes.BOOLEAN,
        ],
        "contains": [
            enums.QuestionTypes.TEXT_INPUT,
            enums.QuestionTypes.TEXT_AREA,
        ],
        "in": [
            enums.QuestionTypes.MULTI_SELECT,
            enums.QuestionTypes.SINGLE_SELECT,
        ],
        "not_in": [
            enums.QuestionTypes.MULTI_SELECT,
            enums.QuestionTypes.SINGLE_SELECT,
        ],
    }
    if question_type in valid_operators[operator]:
        return True

    return False


def _is_valid_trigger_value(
    answer_data: list[str] | str | int | float | bool | datetime.date,
    question_type: str,
) -> bool:
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

    if isinstance(answer_data, int | float) and question_type in [
        enums.QuestionTypes.NUMBER,
    ]:
        return True

    if isinstance(answer_data, bool | type(None)) and question_type in [
        enums.QuestionTypes.BOOLEAN,
    ]:
        return True

    return False


def is_valid_condition_value(
    answer_data: list[str] | str | int | float | bool | datetime.date,
    question_type: str,
) -> bool:
    if isinstance(answer_data, list) and question_type in [
        enums.QuestionTypes.TEXT_INPUT,
        enums.QuestionTypes.TEXT_AREA,
    ]:
        return True

    return _is_valid_trigger_value(answer_data, question_type)


def is_valid_answer(
    answer_data: list[str] | str | int | float | bool | datetime.date,
    question_type: str,
) -> bool:
    if isinstance(answer_data, str) and question_type in [
        enums.QuestionTypes.TEXT_INPUT,
        enums.QuestionTypes.TEXT_AREA,
    ]:
        return True

    return _is_valid_trigger_value(answer_data, question_type)


def apply_operator(user_answer: any, required_value: any, operator: str) -> bool:
    if user_answer is None:
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
