import datetime

from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.media.mixins import ImageModelMixin
from waldur_core.media.validators import ImageValidator
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace import models as marketplace_models

from . import enums, utils


class Category(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
):
    """Groups checklists by category with icon support for UI display."""

    checklists: models.Manager["Checklist"]

    icon = models.FileField(
        upload_to="marketplace_checklist_category_icons",
        blank=True,
        null=True,
        validators=[ImageValidator],
    )

    def __str__(self):
        return self.name

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "Categories"


class Checklist(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
):
    """Main container for compliance questions, associated with customers/roles and typed by compliance area."""

    questions: models.Manager["Question"]

    category = models.ForeignKey(
        to=Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checklists",
    )
    customers = models.ManyToManyField(Customer)
    roles = models.ManyToManyField(to=Role)
    checklist_type = models.CharField(
        max_length=20,
        choices=enums.ChecklistTypes.CHOICES,
        default=enums.ChecklistTypes.PROJECT_COMPLIANCE,
        help_text=_("Type of compliance this checklist addresses"),
    )

    def get_visible_questions(self, user) -> list["Question"]:
        """Get list of questions that should be visible given current answers"""
        visible_questions = []

        for question in self.questions.all().order_by("order"):
            if question.is_visible_for_user(user):
                visible_questions.append(question)

        return visible_questions

    def __str__(self):
        return f"{self.name} ({self.get_checklist_type_display()})"

    class Meta:
        ordering = ("checklist_type", "name")


class Question(core_models.UuidMixin, core_models.DescribableMixin, ImageModelMixin):
    """Individual questions with configurable types, optional images, ordering, and review trigger logic based on answer values."""

    checklist = models.ForeignKey(
        to=Checklist,
        on_delete=models.CASCADE,
        related_name="questions",
    )
    order = models.PositiveIntegerField(default=0)
    category = models.ForeignKey(
        to=marketplace_models.Category,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    solution = models.TextField(
        blank=True,
        null=True,
        help_text=_("Guidance shown when answer needs clarification"),
    )
    required = models.BooleanField(default=False)
    question_type = models.CharField(
        max_length=20,
        choices=enums.QuestionTypes.CHOICES,
        default=enums.QuestionTypes.BOOLEAN,
        help_text=_("Type of question and expected answer format"),
    )

    # Review trigger configuration
    review_answer_value = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Answer value that trigger review."),
        null=True,
    )
    operator = models.CharField(
        max_length=20, choices=enums.OPERATORS, default="equals", blank=True
    )

    always_requires_review = models.BooleanField(
        default=False,
        help_text=_("This question always requires review regardless of answer"),
    )

    class Meta:
        ordering = (
            "checklist",
            "order",
        )

    def is_dependant(self):
        return self.dependencies.exists()

    def is_visible_for_user(self, user):
        if not self.is_dependant():
            return True

        return all(
            [
                dependency.question_is_visible(user)
                for dependency in self.dependencies.all()
            ]
        )

    def is_valid_answer(
        self, answer_data: list[str] | str | int | float | bool | datetime.date | None
    ) -> bool:
        if self.required and answer_data is None:
            return False

        return utils.is_valid_answer(answer_data, self.question_type)

    def should_trigger_review(self, answer_data: any) -> bool | None:
        """Check if this answer should trigger a review"""
        if self.always_requires_review:
            return True

        if self.review_answer_value and self.operator:
            return utils.apply_operator(
                answer_data, self.review_answer_value, self.operator
            )

        return False

    def __str__(self):
        return (
            f"{self.description[:50]}..."
            if len(self.description) > 50
            else self.description
        )


class QuestionOption(core_models.UuidMixin):
    """Multiple choice options for questions with ordering support."""

    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="question_options"
    )
    label = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.question.description[:30]}... - {self.label}"


class QuestionDependency(core_models.UuidMixin, TimeStampedModel):
    """Conditional visibility logic - questions can depend on other questions' answers with circular dependency prevention."""

    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="dependencies"
    )
    depends_on_question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="dependent_questions"
    )
    required_answer_value = models.JSONField(
        help_text=_("The answer value(s) that make this question visible")
    )
    operator = models.CharField(
        max_length=20, choices=enums.OPERATORS, default="equals"
    )

    def __str__(self):
        return f"{self.question.description[:30]}... depends on {self.depends_on_question.description[:30]}..."

    @staticmethod
    def is_circular(question, depends_on_question):
        # Direct self-dependency
        if question == depends_on_question:
            return True

        # Check for circular dependencies through other questions
        visited = set()
        current = depends_on_question

        while current:
            if current in visited:
                return True
            visited.add(current)

            # Find next dependency
            next_dependency = QuestionDependency.objects.filter(
                question=current
            ).first()
            if not next_dependency:
                break

            current = next_dependency.depends_on_question

            # If we reach the original question, we have a cycle
            if current == question:
                return True

        return False

    def question_is_visible(self, user):
        answer_to_base_question = Answer.objects.filter(
            question=self.depends_on_question, user=user
        ).first()
        if not answer_to_base_question:
            return False

        answer = answer_to_base_question.answer_data

        return utils.apply_operator(answer, self.required_answer_value, self.operator)

    class Meta:
        verbose_name = "Question dependency"
        verbose_name_plural = "Question dependencies"
        ordering = ("created",)


class AbstractAnswer(TimeStampedModel):
    """Base class for checklist answers with automatic review flagging and tracking."""

    user = models.ForeignKey(to=core_models.User, on_delete=models.CASCADE)
    question = models.ForeignKey(to=Question, on_delete=models.CASCADE)
    answer_data = models.JSONField(
        default=list,
        help_text=_("Flexible answer storage for different question types"),
    )

    # Review tracking
    requires_review = models.BooleanField(
        default=False,
        editable=False,
        help_text=_("Internal flag - this answer requires additional review"),
    )
    reviewed_by = models.ForeignKey(
        to=core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_%(class)s_answers",  # Use %(class)s for unique related names
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(
        blank=True, help_text=_("Internal notes from reviewer")
    )

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.user.username} - {self.question.description[:30]}..."

    def save(self, *args, **kwargs):
        """Auto-check if review is required when saving"""
        if not self.pk:
            self.requires_review = self.question.should_trigger_review(self.answer_data)
        super().save(*args, **kwargs)


class Answer(AbstractAnswer):
    """User responses stored as JSON with automatic review flagging, reviewer tracking, and unique user-question constraints."""

    class Meta:
        unique_together = ["user", "question"]
