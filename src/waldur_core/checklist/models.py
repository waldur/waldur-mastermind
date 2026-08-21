import base64
import datetime
import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.media import models as media_models
from waldur_core.media import utils as media_utils

from . import enums, utils


class Checklist(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
):
    """Main container for metadata questions."""

    questions: models.Manager["Question"]

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
        ordering = ["checklist_type", "name", "id"]


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

    # Number validation fields (also used for YEAR and RATING types)
    min_value = models.DecimalField(
        max_digits=20,
        decimal_places=4,
        null=True,
        blank=True,
        help_text=_(
            "Minimum value allowed for NUMBER, YEAR, and RATING type questions"
        ),
    )
    max_value = models.DecimalField(
        max_digits=20,
        decimal_places=4,
        null=True,
        blank=True,
        help_text=_(
            "Maximum value allowed for NUMBER, YEAR, and RATING type questions"
        ),
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

    # File validation fields
    allowed_file_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of allowed file extensions (e.g., ['.pdf', '.doc', '.docx']). "
            "If empty, all file types are allowed."
        ),
    )
    allowed_mime_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of allowed MIME types (e.g., ['application/pdf', 'application/msword']). "
            "If empty, MIME type validation is not enforced. When both extensions and MIME types "
            "are specified, files must match both criteria for security."
        ),
    )
    max_file_size_mb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Maximum file size in megabytes. If not set, no size limit is enforced."
        ),
    )
    max_files_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Maximum number of files allowed for MULTIPLE_FILES type questions. "
            "If not set, no count limit is enforced."
        ),
    )

    # Likert scale validation fields
    likert_scale_length = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        choices=enums.LikertScaleLengths.CHOICES,
        help_text=_(
            "Number of points on the Likert scale (3, 5, or 7). "
            "Required for LIKERT type questions."
        ),
    )
    likert_low_label = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text=_(
            "Label for the lowest point on the Likert scale "
            "(e.g. 'Strongly disagree'). Optional."
        ),
    )
    likert_high_label = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text=_(
            "Label for the highest point on the Likert scale "
            "(e.g. 'Strongly agree'). Optional."
        ),
    )
    likert_allow_na = models.BooleanField(
        default=False,
        help_text=_(
            "Allow respondents to choose 'N/A' as an answer for LIKERT type questions."
        ),
    )

    # Rich text validation fields
    rich_text_char_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Maximum number of characters allowed in RICH_TEXT type answers. "
            "If not set, no limit is enforced."
        ),
    )
    rich_text_toolbar_level = models.CharField(
        max_length=10,
        blank=True,
        default=enums.RichTextToolbarLevels.STANDARD,
        choices=enums.RichTextToolbarLevels.CHOICES,
        help_text=_(
            "Toolbar level for the rich text editor: 'minimal', 'standard', or 'extended'."
        ),
    )

    class Meta:
        ordering = ["checklist", "order", "id"]

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

        # Additional validation for NUMBER, YEAR, and RATING types with min/max constraints
        if (
            self.question_type in ["number", "year", "rating"]
            and answer_data is not None
        ):
            # Only apply min/max validation to numeric types (int, float)
            # String values should be handled by the basic type validation first
            # Convert string numbers to float for validation
            try:
                if isinstance(answer_data, str):
                    numeric_value = float(answer_data)
                elif isinstance(answer_data, int | float):
                    numeric_value = float(answer_data)
                else:
                    return False

                # Check minimum value
                if self.min_value is not None and numeric_value < float(self.min_value):
                    return False

                # Check maximum value
                if self.max_value is not None and numeric_value > float(self.max_value):
                    return False
            except (ValueError, TypeError):
                return False

        # Additional validation for FILE and MULTIPLE_FILES type with constraints
        if self.question_type in ["file", "multiple_files"] and answer_data is not None:
            return self.is_valid_file_answer(answer_data)

        # Additional validation for LIKERT type with scale length constraint
        if self.question_type == enums.QuestionTypes.LIKERT and answer_data is not None:
            if answer_data == "na":
                return bool(self.likert_allow_na)
            if not isinstance(answer_data, int) or isinstance(answer_data, bool):
                return False
            scale_length = self.likert_scale_length or enums.LikertScaleLengths.FIVE
            return 0 <= answer_data < scale_length

        # Additional validation for RICH_TEXT type with character limit
        if (
            self.question_type == enums.QuestionTypes.RICH_TEXT
            and answer_data is not None
        ):
            if not isinstance(answer_data, str):
                return False
            if (
                self.rich_text_char_limit is not None
                and len(answer_data) > self.rich_text_char_limit
            ):
                return False

        return True

    def _process_single_file(self, file_data, validate_only=False):
        """Process and validate a single file. Returns processed file data or raises exception if invalid."""
        if not isinstance(file_data, dict):
            raise ValueError("File data must be a dictionary")

        # Check required fields
        required_fields = ["name", "content"]
        if not all(key in file_data for key in required_fields):
            raise ValueError("Missing required fields: name, content")

        # Validate and process base64 content
        content_str = file_data.get("content", "")
        if not content_str:
            raise ValueError("Empty content")

        # Decode base64 content with validation
        file_content = base64.b64decode(content_str, validate=True)
        actual_file_size = len(file_content)

        # Ensure we actually have some content
        if actual_file_size == 0:
            raise ValueError("Empty file content after decoding")

        # Detect MIME type from actual content for security
        import magic

        detected_mime_type = magic.from_buffer(file_content[:1024], mime=True)

        # Check file extension if restrictions are set
        if self.allowed_file_types:
            file_name = file_data.get("name", "")
            file_ext = (
                "." + file_name.split(".")[-1].lower() if "." in file_name else ""
            )
            allowed_extensions = [ext.lower() for ext in self.allowed_file_types]
            if file_ext not in allowed_extensions:
                raise ValueError(f"File extension {file_ext} not allowed")

        # Check MIME type if restrictions are set
        if self.allowed_mime_types:
            if detected_mime_type not in self.allowed_mime_types:
                # Also check for wildcard matches (e.g., 'image/*' matches 'image/jpeg')
                mime_category = detected_mime_type.split("/")[0]
                wildcard_match = any(
                    allowed.endswith("/*") and allowed.startswith(mime_category + "/")
                    for allowed in self.allowed_mime_types
                )
                if not wildcard_match:
                    raise ValueError(f"MIME type {detected_mime_type} not allowed")

        # Check file size if limit is set
        if self.max_file_size_mb:
            max_size_bytes = self.max_file_size_mb * 1024 * 1024
            if actual_file_size > max_size_bytes:
                raise ValueError(
                    f"File size {actual_file_size} exceeds limit {max_size_bytes}"
                )

        if validate_only:
            return True

        # Return processed file metadata (for actual processing)
        return {
            "name": file_data.get("name", ""),
            "size": actual_file_size,
            "mime_type": detected_mime_type,
            "content": file_content,  # Include content for storage
        }

    def is_valid_file_answer(self, answer_data) -> bool:
        """Validate file answer data against file constraints."""
        if self.question_type == "file":
            if not isinstance(answer_data, dict):
                return False
            files = [answer_data]
        elif self.question_type == "multiple_files":
            if not isinstance(answer_data, list):
                return False
            files = answer_data

            # Check max files count
            if self.max_files_count and len(files) > self.max_files_count:
                return False
        else:
            return True

        # Validate each file using the helper method
        try:
            for file_data in files:
                # Skip already-processed files (have stored_file_id, no raw content)
                if (
                    isinstance(file_data, dict)
                    and "stored_file_id" in file_data
                    and "content" not in file_data
                ):
                    continue
                self._process_single_file(file_data, validate_only=True)
            return True
        except Exception:
            return False

    def process_file_answer(self, answer_data):
        """Process file answer data, extracting metadata and storing content."""
        if self.question_type not in ["file", "multiple_files"]:
            return answer_data

        if self.question_type == "file":
            files = [answer_data] if isinstance(answer_data, dict) else []
        else:  # multiple_files
            files = answer_data if isinstance(answer_data, list) else []

        processed_files = []

        for file_data in files:
            # Skip already-processed files (have stored_file_id, no raw content)
            if (
                isinstance(file_data, dict)
                and "stored_file_id" in file_data
                and "content" not in file_data
            ):
                processed_files.append(file_data)
                continue

            try:
                # Use the helper method to process and validate the file
                processed_data = self._process_single_file(
                    file_data, validate_only=False
                )

                # Store content in media storage and create final metadata
                processed_file = {
                    "name": processed_data["name"],
                    "size": processed_data["size"],
                    "mime_type": processed_data["mime_type"],
                    "stored_file_id": self._store_file_content(
                        processed_data["name"] or "unnamed_file",
                        processed_data["content"],
                        processed_data["mime_type"],
                    ),
                }

                processed_files.append(processed_file)

            except Exception:
                # Skip invalid files
                continue

        if self.question_type == "file":
            return processed_files[0] if processed_files else None
        else:  # multiple_files
            return processed_files

    def _store_file_content(self, filename, content, mime_type):
        """Store file content using Waldur's media system."""
        # Generate unique filename

        unique_filename = f"checklist_files/{uuid.uuid4().hex}_{filename}"

        # Create file record
        content_hash = media_utils.get_image_hash(content)
        file_obj = media_models.File.objects.create(
            content=content,
            size=len(content),
            name=unique_filename,
            mime_type=mime_type,
            hash=content_hash,
        )

        return str(file_obj.uuid)

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

    def get_answer_display(self, answer_data: any) -> any:
        """
        Convert answer data to human-readable format.

        For single_select and multi_select questions, resolves UUIDs to labels.
        For other question types, returns the answer data as-is.

        Args:
            answer_data: Raw answer data (may contain UUIDs for select-type questions)

        Returns:
            Human-readable answer data (labels for select-type questions, original data for others)
        """
        if self.question_type not in [
            enums.QuestionTypes.SINGLE_SELECT,
            enums.QuestionTypes.MULTI_SELECT,
        ]:
            return answer_data

        if answer_data is None:
            return None

        options_map = {str(opt.uuid): opt.label for opt in self.question_options.all()}

        if self.question_type == enums.QuestionTypes.SINGLE_SELECT:
            # Single select is stored as a list with one element
            if isinstance(answer_data, list) and len(answer_data) > 0:
                uuid_val = answer_data[0]
                return options_map.get(str(uuid_val), str(uuid_val))
            return options_map.get(str(answer_data), str(answer_data))

        elif self.question_type == enums.QuestionTypes.MULTI_SELECT:
            if not isinstance(answer_data, list):
                return answer_data

            return [
                options_map.get(str(uuid_val), str(uuid_val))
                for uuid_val in answer_data
            ]

        return answer_data

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
        ordering = ["order", "id"]

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
        if not completion.pk:
            return False

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
        ordering = ["created", "id"]


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
        ordering = ["-modified", "id"]

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
        """Auto-check if review is required and process file content when saving"""
        # Process file content on both create and update.
        # process_file_answer is idempotent: already-processed files (with stored_file_id)
        # are skipped, so re-saving a processed answer is safe.
        if self.question.question_type in ["file", "multiple_files"]:
            self.answer_data = self.question.process_file_answer(self.answer_data)

        if not self.pk:
            self.requires_review = self.question.should_trigger_review(self.answer_data)

        super().save(*args, **kwargs)

        # Update completion status (only if completion is set)
        if self.completion:
            self.completion.update_completion_status()
