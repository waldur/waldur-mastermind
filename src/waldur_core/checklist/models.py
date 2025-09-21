import datetime

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.media.validators import ImageValidator

from . import enums, utils


class Category(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
):
    """Groups checklists by category with icon support for UI display."""

    checklists: models.Manager["Checklist"]

    icon = models.FileField(
        upload_to="checklist_category_icons",
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
    """Main container for metadata questions."""

    questions: models.Manager["Question"]

    category = models.ForeignKey(
        to=Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checklists",
    )
    checklist_type = models.CharField(
        max_length=20,
        choices=enums.ChecklistTypes.CHOICES,
        default=enums.ChecklistTypes.PROJECT_COMPLIANCE,
        help_text=_("Type of compliance this checklist addresses"),
    )

    def get_visible_questions(self, completion) -> list["Question"]:
        """Get list of questions that should be visible given current answers in completion context"""
        visible_questions = []

        for question in self.questions.all().order_by("order"):
            if question.is_visible_for_completion(completion):
                visible_questions.append(question)

        return visible_questions

    def __str__(self):
        return f"{self.name} ({self.get_checklist_type_display()})"

    class Meta:
        ordering = ("checklist_type", "name")


class Question(core_models.UuidMixin, core_models.DescribableMixin):
    """Individual questions with configurable types, ordering, and review trigger logic based on answer values."""

    checklist = models.ForeignKey(
        to=Checklist,
        on_delete=models.CASCADE,
        related_name="questions",
    )
    order = models.PositiveIntegerField(default=0)
    required = models.BooleanField(default=False)
    question_type = models.CharField(
        max_length=20,
        choices=enums.QuestionTypes.CHOICES,
        default=enums.QuestionTypes.BOOLEAN,
        help_text=_("Type of question and expected answer format"),
    )
    user_guidance = models.TextField(
        blank=True,
        help_text=_(
            "Additional guidance text visible to users when answering and reviewing"
        ),
    )

    # Conditional user guidance configuration
    guidance_answer_value = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Answer value that triggers display of user guidance."),
        null=True,
    )
    guidance_operator = models.CharField(
        max_length=20,
        choices=enums.OPERATORS,
        default="equals",
        blank=True,
        help_text=_("Operator to use when comparing answer with guidance_answer_value"),
    )

    always_show_guidance = models.BooleanField(
        default=True,
        help_text=_(
            "Show user guidance always, regardless of answer. If False, guidance is conditional on answer matching guidance_answer_value with guidance_operator"
        ),
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

    # Number validation fields
    min_value = models.DecimalField(
        max_digits=20,
        decimal_places=4,
        null=True,
        blank=True,
        help_text=_("Minimum value allowed for NUMBER type questions"),
    )
    max_value = models.DecimalField(
        max_digits=20,
        decimal_places=4,
        null=True,
        blank=True,
        help_text=_("Maximum value allowed for NUMBER type questions"),
    )

    # Dependency logic operator
    dependency_logic_operator = models.CharField(
        max_length=10,
        choices=enums.DependencyLogicOperators.CHOICES,
        default=enums.DependencyLogicOperators.AND,
        help_text=_(
            "Defines how multiple dependencies are evaluated. "
            "AND: All dependencies must be satisfied. "
            "OR: At least one dependency must be satisfied."
        ),
    )

    class Meta:
        ordering = (
            "checklist",
            "order",
        )

    def is_dependant(self):
        return self.dependencies.exists()

    def is_visible_for_completion(self, completion):
        """Check if question is visible in the given completion context"""
        if not self.is_dependant():
            return True

        dependencies_results = [
            dependency.question_is_visible(completion)
            for dependency in self.dependencies.all()
        ]

        if self.dependency_logic_operator == enums.DependencyLogicOperators.OR:
            return any(dependencies_results)
        else:  # Default to AND logic
            return all(dependencies_results)

    def is_valid_answer(
        self, answer_data: list[str] | str | int | float | bool | datetime.date | None
    ) -> bool:
        if self.required and answer_data is None:
            return False

        # First check basic type validation
        if not utils.is_valid_answer(answer_data, self.question_type):
            return False

        # Additional validation for NUMBER type with min/max constraints
        if self.question_type == "number" and answer_data is not None:
            # Only apply min/max validation to numeric types (int, float)
            # String values should be handled by the basic type validation first
            if isinstance(answer_data, int | float):
                numeric_value = float(answer_data)

                # Check minimum value
                if self.min_value is not None and numeric_value < float(self.min_value):
                    return False

                # Check maximum value
                if self.max_value is not None and numeric_value > float(self.max_value):
                    return False

        return True

    def should_trigger_review(self, answer_data: any) -> bool | None:
        """Check if this answer should trigger a review"""
        if self.always_requires_review:
            return True

        if self.review_answer_value and self.operator:
            return utils.apply_operator(
                answer_data, self.review_answer_value, self.operator
            )

        return False

    def should_show_guidance(self, answer_data: any) -> bool:
        """Check if user guidance should be shown for this answer"""
        if self.always_show_guidance:
            return bool(self.user_guidance.strip())

        if (
            self.guidance_answer_value is not None
            and self.guidance_operator
            and self.user_guidance.strip()
        ):
            return utils.apply_operator(
                answer_data, self.guidance_answer_value, self.guidance_operator
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

    def question_is_visible(self, completion):
        """Check if dependency condition is satisfied in the completion context"""
        answer_to_base_question = completion.answers.filter(
            question=self.depends_on_question
        ).first()

        if not answer_to_base_question:
            return False

        answer = answer_to_base_question.answer_data

        return utils.apply_operator(answer, self.required_answer_value, self.operator)

    class Meta:
        verbose_name = "Question dependency"
        verbose_name_plural = "Question dependencies"
        ordering = ("created",)


class ChecklistCompletion(
    core_models.UuidMixin,
    TimeStampedModel,
):
    """Generic checklist completion tracking for any domain model."""

    # Reference to the checklist being completed
    checklist = models.ForeignKey(
        Checklist, on_delete=models.CASCADE, help_text=_("Checklist being completed")
    )

    # Generic foreign key to the domain object (e.g., Proposal, Project, etc.)
    scope_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        help_text=_("Type of object this completion belongs to"),
    )
    scope_object_id = models.PositiveIntegerField(
        help_text=_("ID of the object this completion belongs to")
    )
    scope = GenericForeignKey("scope_content_type", "scope_object_id")

    # Completion status fields
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

    class Meta:
        unique_together = ["scope_content_type", "scope_object_id", "checklist"]
        verbose_name = "Checklist completion"
        verbose_name_plural = "Checklist completions"
        ordering = ["-modified"]

    def __str__(self):
        return f"{self.scope} - {self.checklist.name}"

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


class Answer(core_models.UuidMixin, TimeStampedModel):
    """Checklist answers linked to completion objects."""

    user = models.ForeignKey(to=core_models.User, on_delete=models.CASCADE)
    question = models.ForeignKey(to=Question, on_delete=models.CASCADE)
    answer_data = models.JSONField(
        default=list,
        help_text=_("Flexible answer storage for different question types"),
    )

    completion = models.ForeignKey(
        ChecklistCompletion,
        on_delete=models.CASCADE,
        null=True,  # this should never happen, completion is expected to be created by specific app
        related_name="answers",
        help_text=_("Checklist completion this answer belongs to"),
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
        related_name="reviewed_answers",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(
        blank=True, help_text=_("Internal notes from reviewer")
    )

    class Meta:
        unique_together = ["completion", "question", "user"]

    def __str__(self):
        return f"{self.user.username} - {self.question.description[:30]}..."

    def save(self, *args, **kwargs):
        """Auto-check if review is required when saving"""
        if not self.pk:
            self.requires_review = self.question.should_trigger_review(self.answer_data)

        super().save(*args, **kwargs)

        # Update completion status (only if completion is set)
        if self.completion:
            self.completion.update_completion_status()
