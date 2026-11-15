from django.db import transaction

from . import models, tasks


def _recompute_completions(checklist_id):
    # A checklist nobody has answered yet, such as one being built or
    # imported, has nothing to recompute.
    if not models.ChecklistCompletion.objects.filter(
        checklist_id=checklist_id
    ).exists():
        return
    transaction.on_commit(
        lambda: tasks.recompute_checklist_completions.delay(checklist_id)
    )


def remember_question_visibility_fields(sender, instance, raw=False, **kwargs):
    """Keep the fields completion depends on, to tell on save whether they changed."""
    if raw or not instance.pk:
        instance._previous_completion_fields = None
        return
    instance._previous_completion_fields = (
        models.Question.objects.filter(pk=instance.pk)
        .values_list("required", "dependency_logic_operator")
        .first()
    )


def recompute_completions_on_question_change(
    sender, instance, created=False, raw=False, **kwargs
):
    """A question that becomes required, or whose dependencies combine
    differently, changes which completions are complete."""
    if raw:
        return
    previous = getattr(instance, "_previous_completion_fields", None)
    current = (instance.required, instance.dependency_logic_operator)
    if (created and instance.required) or (previous and previous != current):
        _recompute_completions(instance.checklist_id)


def recompute_completions_on_question_delete(sender, instance, **kwargs):
    _recompute_completions(instance.checklist_id)


def recompute_completions_on_dependency_change(sender, instance, raw=False, **kwargs):
    """Adding, changing or removing a dependency changes which questions are shown."""
    if raw:
        return
    # Read the checklist anew: when the question itself is being deleted, its
    # own handler covers the checklist.
    checklist_id = (
        models.Question.objects.filter(pk=instance.question_id)
        .values_list("checklist_id", flat=True)
        .first()
    )
    if checklist_id is not None:
        _recompute_completions(checklist_id)
