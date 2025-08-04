from django.db import models
from django.utils.translation import gettext_lazy as _

from waldur_core.core import models as core_models


class ChecklistCompletionMixin(models.Model):
    """Abstract mixin providing checklist completion functionality for any model that tracks checklist completion."""

    class Meta:
        abstract = True

    is_completed = models.BooleanField(
        default=False, help_text=_("Whether all required questions have been answered")
    )

    requires_review = models.BooleanField(
        default=False, help_text=_("Whether any answers triggered review requirements")
    )

    reviewed_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=_("User who reviewed the checklist completion"),
    )

    reviewed_at = models.DateTimeField(null=True, blank=True)

    review_notes = models.TextField(blank=True, help_text=_("Notes from the reviewer"))

    def update_completion_status(self):
        """Update completion and review status based on answers."""
        # Check if all required questions are answered
        required_questions = self.checklist.questions.filter(required=True)
        answered_question_ids = self.answers.values_list("question_id", flat=True)
        self.is_completed = all(
            q.id in answered_question_ids for q in required_questions
        )

        # Check if any answers require review
        self.requires_review = self.answers.filter(requires_review=True).exists()

        self.save()

    def get_completion_percentage(self):
        """Calculate completion percentage."""
        total_questions = self.checklist.questions.count()
        if total_questions == 0:
            return 100

        answered_questions = self.answers.count()
        return round((answered_questions / total_questions) * 100, 1)

    def get_review_trigger_summary(self):
        """Get summary of answers that triggered review."""
        review_answers = self.answers.filter(requires_review=True).select_related(
            "question"
        )

        return [
            {
                "question": answer.question.description,
                "answer": answer.answer_data,
                "trigger_value": answer.question.review_answer_value,
                "operator": answer.question.operator,
            }
            for answer in review_answers
        ]

    def get_unanswered_required_questions(self):
        """Get list of required questions that haven't been answered yet."""
        answered_question_ids = self.answers.values_list("question_id", flat=True)

        return self.checklist.questions.filter(required=True).exclude(
            id__in=answered_question_ids
        )

    def get_questions_requiring_review(self):
        """Get list of questions whose answers triggered review requirements."""
        return self.answers.filter(requires_review=True).values_list(
            "question", flat=True
        )
