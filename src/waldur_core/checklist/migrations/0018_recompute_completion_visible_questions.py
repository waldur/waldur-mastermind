"""Recompute is_completed and requires_review now that questions hidden by
their dependencies no longer count.

Only checklists with dependencies can change: elsewhere every question is
shown, as before. The rules are copied here so that later changes to the
checklist code do not change what this migration does.
"""

from collections import defaultdict

from django.db import migrations


def _answers_equal(answer, value):
    if isinstance(answer, list) and isinstance(value, list):
        try:
            return set(answer) == set(value)
        except TypeError:
            pass
    return answer == value


def _condition_met(answer, value, operator):
    if answer is None:
        return False
    if operator == "equals":
        return _answers_equal(answer, value)
    if operator == "not_equals":
        return not _answers_equal(answer, value)
    if operator == "contains":
        return any(item in answer for item in value)
    items = answer if isinstance(answer, list) else [answer]
    if operator == "in":
        if isinstance(value, list):
            return any(item in value for item in items)
        return answer == value
    if operator == "not_in":
        if isinstance(value, list):
            return not any(item in value for item in items)
        return answer != value
    return False


def _latest_answers(answers):
    latest = {}
    for answer in answers:
        current = latest.get(answer.question_id)
        if current is None or (answer.modified, answer.id) > (
            current.modified,
            current.id,
        ):
            latest[answer.question_id] = answer
    return latest


def _visible_ids(questions, dependencies, answers):
    by_id = {question.id: question for question in questions}
    visible = {}

    def met(dependency):
        answer = answers.get(dependency.depends_on_question_id)
        parent = by_id.get(dependency.depends_on_question_id)
        return (
            answer is not None
            and parent is not None
            and _condition_met(
                answer.answer_data,
                dependency.required_answer_value,
                dependency.operator,
            )
            and is_visible(parent)
        )

    def is_visible(question):
        if question.id not in visible:
            visible[question.id] = False
            own = dependencies.get(question.id)
            if not own:
                visible[question.id] = True
            elif question.dependency_logic_operator == "or":
                visible[question.id] = any(map(met, own))
            else:
                visible[question.id] = all(map(met, own))
        return visible[question.id]

    return {question.id for question in questions if is_visible(question)}


def recompute_completion(apps, schema_editor):
    Question = apps.get_model("checklist", "Question")
    QuestionDependency = apps.get_model("checklist", "QuestionDependency")
    ChecklistCompletion = apps.get_model("checklist", "ChecklistCompletion")
    Answer = apps.get_model("checklist", "Answer")

    dependencies_by_checklist = defaultdict(lambda: defaultdict(list))
    for dependency in QuestionDependency.objects.select_related("question"):
        dependencies_by_checklist[dependency.question.checklist_id][
            dependency.question_id
        ].append(dependency)

    for checklist_id, dependencies in dependencies_by_checklist.items():
        questions = list(Question.objects.filter(checklist_id=checklist_id))
        completions = ChecklistCompletion.objects.filter(checklist_id=checklist_id)
        for completion in completions.iterator():
            answers = _latest_answers(
                Answer.objects.filter(completion_id=completion.id)
            )
            visible = _visible_ids(questions, dependencies, answers)
            is_completed = all(
                question.id in answers
                for question in questions
                if question.required and question.id in visible
            )
            requires_review = any(
                answer.requires_review
                for question_id, answer in answers.items()
                if question_id in visible
            )
            if (is_completed, requires_review) != (
                completion.is_completed,
                completion.requires_review,
            ):
                ChecklistCompletion.objects.filter(pk=completion.pk).update(
                    is_completed=is_completed, requires_review=requires_review
                )


class Migration(migrations.Migration):
    dependencies = [
        ("checklist", "0017_checklist_organization"),
    ]

    operations = [
        migrations.RunPython(recompute_completion, migrations.RunPython.noop),
    ]
