from django.apps import AppConfig
from django.db.models import signals


class ChecklistConfig(AppConfig):
    name = "waldur_core.checklist"
    verbose_name = "Checklist"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from waldur_core.checklist import handlers
        from waldur_core.checklist.models import Question, QuestionDependency

        signals.pre_save.connect(
            handlers.remember_question_visibility_fields,
            sender=Question,
            dispatch_uid="waldur_core.checklist.handlers.remember_question_visibility_fields",
        )
        signals.post_save.connect(
            handlers.recompute_completions_on_question_change,
            sender=Question,
            dispatch_uid="waldur_core.checklist.handlers.recompute_completions_on_question_change",
        )
        signals.post_delete.connect(
            handlers.recompute_completions_on_question_delete,
            sender=Question,
            dispatch_uid="waldur_core.checklist.handlers.recompute_completions_on_question_delete",
        )
        signals.post_save.connect(
            handlers.recompute_completions_on_dependency_change,
            sender=QuestionDependency,
            dispatch_uid="waldur_core.checklist.handlers.recompute_completions_on_dependency_save",
        )
        signals.post_delete.connect(
            handlers.recompute_completions_on_dependency_change,
            sender=QuestionDependency,
            dispatch_uid="waldur_core.checklist.handlers.recompute_completions_on_dependency_delete",
        )
