from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core.openapi_extensions import AnyJSONField

from . import enums, models, utils


class ChecklistSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    questions_count = serializers.IntegerField(source="questions.count", read_only=True)

    class Meta:
        model = models.Checklist
        view_name = "checklists-admin-detail"
        fields = [
            "uuid",
            "url",
            "name",
            "description",
            "checklist_type",
            "questions_count",
        ]

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }

    def validate_checklist_type(self, value):
        """
        Validate that only one checklist of type ONBOARDING_CUSTOMER_DATA or
        ONBOARDING_INTENT_DATA can exist in the database.
        """
        # Only validate for onboarding checklist types
        if value in [
            enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
            enums.ChecklistTypes.ONBOARDING_INTENT_DATA,
        ]:
            queryset = models.Checklist.objects.filter(checklist_type=value)

            if not self.instance and queryset.exists():
                raise serializers.ValidationError(
                    f"'{value}' type checklist already exists. "
                    f"Please either update the existing checklist or remove to create a new one."
                )

        return value


class QuestionOptionsSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.QuestionOption
        fields = ("uuid", "label", "order")


class QuestionOptionsAdminSerializer(QuestionOptionsSerializer):
    question = serializers.HyperlinkedRelatedField(
        queryset=models.Question.objects.all(),
        required=True,
        view_name="checklists-admin-questions-detail",
        lookup_field="uuid",
    )
    question_uuid = serializers.UUIDField(source="question.uuid", read_only=True)

    class Meta(QuestionOptionsSerializer.Meta):
        view_name = "checklists-admin-question-options-detail"
        fields = QuestionOptionsSerializer.Meta.fields + (
            "url",
            "question",
            "question_uuid",
        )
        protected_fields = ("question",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }


class QuestionDependencySerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    required_answer_value = AnyJSONField(required=False)
    question = serializers.HyperlinkedRelatedField(
        queryset=models.Question.objects.all(),
        required=True,
        view_name="checklists-admin-questions-detail",
        lookup_field="uuid",
    )
    question_name = serializers.CharField(source="question.description", read_only=True)
    depends_on_question = serializers.HyperlinkedRelatedField(
        queryset=models.Question.objects.all(),
        required=True,
        view_name="checklists-admin-questions-detail",
        lookup_field="uuid",
    )
    depends_on_question_name = serializers.CharField(
        source="depends_on_question.description", read_only=True
    )

    def validate(self, attrs):
        question = attrs.get("question")
        depends_on_question = attrs.get("depends_on_question")
        required_answer_value = attrs.get("required_answer_value")
        operator = attrs.get("operator")

        # Check for circular dependencies
        if models.QuestionDependency.is_circular(question, depends_on_question):
            raise serializers.ValidationError("Question cannot depend on itself")

        # Validate required answer value
        if not utils.is_valid_condition_value(
            required_answer_value, depends_on_question.question_type
        ):
            raise serializers.ValidationError(
                f"Required answer value '{required_answer_value}' is not valid for the question '{depends_on_question}' (type: {depends_on_question.question_type})."
            )

        # Validate operator
        if not utils.is_valid_operator_for_question_type(
            depends_on_question.question_type, operator
        ):
            raise serializers.ValidationError(
                f"Invalid operator '{operator}' for the question '{depends_on_question}' (type: {depends_on_question.question_type})."
            )

        return attrs

    class Meta:
        model = models.QuestionDependency
        view_name = "checklists-admin-question-dependencies-detail"
        fields = (
            "uuid",
            "url",
            "question",
            "question_name",
            "depends_on_question",
            "depends_on_question_name",
            "required_answer_value",
            "operator",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }


class QuestionSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    review_answer_value = AnyJSONField(required=False)
    allowed_file_types = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    allowed_mime_types = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    question_options = QuestionOptionsSerializer(many=True, read_only=True)

    class Meta:
        model = models.Question
        fields = [
            "uuid",
            "required",
            "description",
            "user_guidance",
            "question_options",
            "question_type",
            "order",
            "min_value",
            "max_value",
            "allowed_file_types",
            "allowed_mime_types",
            "max_file_size_mb",
            "max_files_count",
            "likert_scale_length",
            "likert_low_label",
            "likert_high_label",
            "likert_allow_na",
            "rich_text_char_limit",
            "rich_text_toolbar_level",
            "operator",
            "review_answer_value",
            "always_requires_review",
            "guidance_answer_value",
            "guidance_operator",
            "always_show_guidance",
            "dependency_logic_operator",
        ]


class AnswerSerializer(serializers.ModelSerializer):
    """Comprehensive serializer for checklist answers with question details."""

    question_uuid = serializers.UUIDField(write_only=True)
    question_description = serializers.CharField(
        source="question.description", read_only=True
    )
    question_type = serializers.CharField(
        source="question.question_type", read_only=True
    )
    question_required = serializers.BooleanField(
        source="question.required", read_only=True
    )
    user_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = models.Answer
        fields = (
            "uuid",
            "question_uuid",
            "question_description",
            "question_type",
            "question_required",
            "answer_data",
            "requires_review",
            "user",
            "user_name",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "question_description",
            "question_type",
            "question_required",
            "requires_review",
            "user",
            "user_name",
            "created",
            "modified",
        )


class QuestionConditionSerializer(serializers.Serializer):
    question_description = serializers.CharField()
    operator = serializers.CharField()
    required_value = serializers.JSONField()


class QuestionDependencyInfoSerializer(serializers.Serializer):
    logic = serializers.CharField()
    conditions = QuestionConditionSerializer(many=True)


class QuestionWithAnswerSerializer(serializers.ModelSerializer):
    """Generic serializer for questions with existing answer context (basic view - no review logic)."""

    existing_answer = serializers.SerializerMethodField()
    question_options = serializers.SerializerMethodField()
    user_guidance = serializers.SerializerMethodField()
    dependencies_info = serializers.SerializerMethodField()

    class Meta:
        model = models.Question
        fields = (
            "uuid",
            "description",
            "user_guidance",
            "question_type",
            "required",
            "order",
            "existing_answer",
            "question_options",
            "min_value",
            "max_value",
            "allowed_file_types",
            "allowed_mime_types",
            "max_file_size_mb",
            "max_files_count",
            "likert_scale_length",
            "likert_low_label",
            "likert_high_label",
            "likert_allow_na",
            "rich_text_char_limit",
            "rich_text_toolbar_level",
            "dependencies_info",
        )
        read_only_fields = (
            "uuid",
            "description",
            "user_guidance",
            "question_type",
            "required",
            "order",
            "existing_answer",
            "question_options",
            "min_value",
            "max_value",
            "allowed_file_types",
            "allowed_mime_types",
            "max_file_size_mb",
            "max_files_count",
            "likert_scale_length",
            "likert_low_label",
            "likert_high_label",
            "likert_allow_na",
            "rich_text_char_limit",
            "rich_text_toolbar_level",
            "dependencies_info",
        )

    @extend_schema_field(AnswerSerializer(allow_null=True))
    def get_existing_answer(self, obj):
        """Get existing answer for this question in the current completion context."""
        request = self.context.get("request")
        completion = self.context.get("completion")

        if not request or not completion:
            return None

        try:
            # If user has permission to view the completion, they should see all answers
            # The permission check is done at the viewset level, so if we're here, user is authorized
            answer = completion.answers.filter(question=obj).first()

            if not answer:
                return None

            # For basic view, hide review flag from answer
            answer_data = AnswerSerializer(answer, context=self.context).data
            if hasattr(self, "_hide_review_flags") or not isinstance(
                self, QuestionWithAnswerReviewerSerializer
            ):
                answer_data.pop("requires_review", None)
            return answer_data
        except models.Answer.DoesNotExist:
            return None

    @extend_schema_field(serializers.ListField(allow_null=True))
    def get_question_options(self, obj):
        """Get question options for select-type questions."""
        if obj.question_type in ["single_select", "multi_select"]:
            return [
                {
                    "uuid": str(option.uuid),
                    "label": option.label,
                    "order": option.order,
                }
                for option in obj.question_options.all().order_by("order")
            ]
        return []

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_guidance(self, obj):
        """Get conditional user guidance based on existing answer."""
        request = self.context.get("request")
        completion = self.context.get("completion")

        if not request or not completion:
            # No answer context, show guidance only if always_show_guidance is True
            if obj.always_show_guidance:
                return obj.user_guidance if obj.user_guidance.strip() else None
            return None

        try:
            answer = completion.answers.get(question=obj, user=request.user)
            answer_data = answer.answer_data

            # Check if guidance should be shown for this answer
            if obj.should_show_guidance(answer_data):
                return obj.user_guidance if obj.user_guidance.strip() else None
            return None

        except models.Answer.DoesNotExist:
            # No answer yet, show guidance only if always_show_guidance is True
            if obj.always_show_guidance:
                return obj.user_guidance if obj.user_guidance.strip() else None
            return None

    @extend_schema_field(QuestionDependencyInfoSerializer(allow_null=True))
    def get_dependencies_info(self, obj):
        """Return dependency information for conditional questions."""
        if not obj.is_dependant():
            return None

        dependencies = obj.dependencies.select_related("depends_on_question").all()

        return {
            "logic": obj.dependency_logic_operator,
            "conditions": [
                {
                    "question_description": dep.depends_on_question.description,
                    "operator": dep.operator,
                    "required_value": dep.required_answer_value,
                }
                for dep in dependencies
            ],
        }


class QuestionWithAnswerReviewerSerializer(QuestionWithAnswerSerializer):
    """Extended serializer for questions with review logic (reviewer view)."""

    review_answer_value = AnyJSONField(required=False)

    class Meta(QuestionWithAnswerSerializer.Meta):
        fields = QuestionWithAnswerSerializer.Meta.fields + (
            "operator",
            "review_answer_value",
            "always_requires_review",
        )


class QuestionAdminSerializer(QuestionSerializer):
    question_options = QuestionOptionsAdminSerializer(many=True, read_only=True)
    checklist = serializers.HyperlinkedRelatedField(
        queryset=models.Checklist.objects.all(),
        required=True,
        view_name="checklists-admin-detail",
        lookup_field="uuid",
    )
    checklist_name = serializers.CharField(read_only=True, source="checklist.name")
    checklist_uuid = serializers.UUIDField(read_only=True, source="checklist.uuid")
    checklist_type = serializers.CharField(
        read_only=True, source="checklist.checklist_type"
    )

    def validate(self, attrs):
        operator = attrs.get("operator")
        review_answer_value = attrs.get("review_answer_value")
        guidance_operator = attrs.get("guidance_operator")
        guidance_answer_value = attrs.get("guidance_answer_value")
        always_show_guidance = attrs.get("always_show_guidance")
        question_type = attrs.get("question_type")
        min_value = attrs.get("min_value")
        max_value = attrs.get("max_value")
        dependency_logic_operator = attrs.get("dependency_logic_operator")
        allowed_file_types = attrs.get("allowed_file_types")
        allowed_mime_types = attrs.get("allowed_mime_types")
        max_file_size_mb = attrs.get("max_file_size_mb")
        max_files_count = attrs.get("max_files_count")
        likert_scale_length = attrs.get("likert_scale_length")
        likert_low_label = attrs.get("likert_low_label")
        likert_high_label = attrs.get("likert_high_label")
        likert_allow_na = attrs.get("likert_allow_na")
        rich_text_char_limit = attrs.get("rich_text_char_limit")
        rich_text_toolbar_level = attrs.get("rich_text_toolbar_level")

        # For PATCH, question_type may be absent from payload — fall back to the
        # currently-persisted value so per-type whitelists still apply.
        effective_question_type = question_type or (
            self.instance.question_type if self.instance else None
        )

        # Validate review trigger configuration
        # Check if both operator and review_answer_value are set together or both empty
        if bool(operator) != bool(review_answer_value):
            raise serializers.ValidationError(
                "Both 'operator' and 'review_answer_value' must be set together or both must be empty."
            )

        # Validate operator for question type
        if (
            operator
            and question_type
            and not utils.is_valid_operator_for_question_type(question_type, operator)
        ):
            raise serializers.ValidationError(
                f"Operator '{operator}' is not valid for question type '{question_type}'."
            )

        # Validate review answer value for question type
        if (
            review_answer_value
            and question_type
            and not utils.is_valid_condition_value(review_answer_value, question_type)
        ):
            raise serializers.ValidationError(
                f"Review answer value '{review_answer_value}' is not valid for question type '{question_type}'."
            )

        # Validate guidance configuration
        # If always_show_guidance is False, then guidance_operator and guidance_answer_value must be set
        if always_show_guidance is False:  # explicitly check for False, not just falsy
            if not guidance_operator or guidance_answer_value is None:
                raise serializers.ValidationError(
                    "When 'always_show_guidance' is False, both 'guidance_operator' and 'guidance_answer_value' must be set."
                )

        # Validate guidance operator for question type
        if (
            guidance_operator
            and question_type
            and not utils.is_valid_operator_for_question_type(
                question_type, guidance_operator
            )
        ):
            raise serializers.ValidationError(
                f"Guidance operator '{guidance_operator}' is not valid for question type '{question_type}'."
            )

        # Validate guidance answer value for question type
        if (
            guidance_answer_value
            and question_type
            and not utils.is_valid_condition_value(guidance_answer_value, question_type)
        ):
            raise serializers.ValidationError(
                f"Guidance answer value '{guidance_answer_value}' is not valid for question type '{question_type}'."
            )

        # Validate min/max values for NUMBER, YEAR, and RATING questions only
        numeric_types = ["number", "year", "rating"]
        if effective_question_type and effective_question_type not in numeric_types:
            if min_value is not None or max_value is not None:
                raise serializers.ValidationError(
                    "Min and max values can only be set for NUMBER, YEAR, and RATING type questions."
                )

        # Validate that min_value is not greater than max_value
        if min_value is not None and max_value is not None and min_value > max_value:
            raise serializers.ValidationError(
                "Minimum value cannot be greater than maximum value."
            )

        # Validate dependency_logic_operator
        if dependency_logic_operator and dependency_logic_operator not in [
            choice[0] for choice in enums.DependencyLogicOperators.CHOICES
        ]:
            raise serializers.ValidationError(
                f"Invalid dependency logic operator: {dependency_logic_operator}. "
                f"Must be one of: {[choice[0] for choice in enums.DependencyLogicOperators.CHOICES]}"
            )

        # Validate file-specific fields for FILE and MULTIPLE_FILES questions only
        if effective_question_type and effective_question_type not in [
            "file",
            "multiple_files",
        ]:
            if (
                allowed_file_types
                or allowed_mime_types
                or max_file_size_mb is not None
                or max_files_count is not None
            ):
                raise serializers.ValidationError(
                    "File validation fields (allowed_file_types, allowed_mime_types, max_file_size_mb, max_files_count) "
                    "can only be set for FILE or MULTIPLE_FILES type questions."
                )

        # Validate allowed_file_types format
        if allowed_file_types is not None:
            if not isinstance(allowed_file_types, list):
                raise serializers.ValidationError(
                    "allowed_file_types must be a list of file extensions."
                )

            for file_type in allowed_file_types:
                if not isinstance(file_type, str) or not file_type.startswith("."):
                    raise serializers.ValidationError(
                        f"Invalid file type '{file_type}'. File types must be strings starting with '.' (e.g., '.pdf', '.doc')."
                    )

        # Validate allowed_mime_types format
        if allowed_mime_types is not None:
            if not isinstance(allowed_mime_types, list):
                raise serializers.ValidationError(
                    "allowed_mime_types must be a list of MIME types."
                )

            for mime_type in allowed_mime_types:
                if not isinstance(mime_type, str):
                    raise serializers.ValidationError(
                        f"Invalid MIME type '{mime_type}'. MIME types must be strings."
                    )

                # Basic MIME type format validation
                if "/" not in mime_type and not mime_type.endswith("/*"):
                    raise serializers.ValidationError(
                        f"Invalid MIME type '{mime_type}'. MIME types must be in format 'type/subtype' or 'type/*' (e.g., 'application/pdf', 'image/*')."
                    )

        # Validate max_file_size_mb
        if max_file_size_mb is not None and max_file_size_mb <= 0:
            raise serializers.ValidationError(
                "max_file_size_mb must be a positive number."
            )

        # Validate max_files_count
        if max_files_count is not None:
            if max_files_count <= 0:
                raise serializers.ValidationError(
                    "max_files_count must be a positive number."
                )
            if effective_question_type == "file":
                raise serializers.ValidationError(
                    "max_files_count can only be set for MULTIPLE_FILES type questions, not FILE type."
                )

        # Validate Likert-specific fields are only set for LIKERT questions
        if (
            effective_question_type
            and effective_question_type != enums.QuestionTypes.LIKERT
        ):
            if (
                likert_scale_length is not None
                or likert_low_label
                or likert_high_label
                or likert_allow_na
            ):
                raise serializers.ValidationError(
                    "Likert fields (likert_scale_length, likert_low_label, likert_high_label, "
                    "likert_allow_na) can only be set for LIKERT type questions."
                )

        # When question_type is LIKERT, scale length must be one of the allowed values
        if question_type == enums.QuestionTypes.LIKERT and likert_scale_length is None:
            raise serializers.ValidationError(
                "likert_scale_length is required for LIKERT type questions."
            )

        # Validate rich-text-specific fields are only set for RICH_TEXT questions
        if (
            effective_question_type
            and effective_question_type != enums.QuestionTypes.RICH_TEXT
        ):
            if rich_text_char_limit is not None or rich_text_toolbar_level:
                raise serializers.ValidationError(
                    "Rich text fields (rich_text_char_limit, rich_text_toolbar_level) "
                    "can only be set for RICH_TEXT type questions."
                )

        if rich_text_char_limit is not None and rich_text_char_limit <= 0:
            raise serializers.ValidationError(
                "rich_text_char_limit must be a positive number."
            )

        return attrs

    class Meta(QuestionSerializer.Meta):
        view_name = "checklists-admin-questions-detail"
        fields = QuestionSerializer.Meta.fields + [
            "url",
            "checklist_name",
            "checklist_uuid",
            "checklist_type",
            "checklist",
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }


class AnswerSubmitSerializer(serializers.Serializer):
    """Generic serializer for submitting checklist answers."""

    question_uuid = serializers.UUIDField()
    answer_data = AnyJSONField(allow_null=True)

    def validate(self, attrs):
        """Validate answer data using the same logic as model's clean method"""
        question_uuid = attrs.get("question_uuid")
        answer_data = attrs.get("answer_data")

        # Get question instance for validation
        try:
            question = models.Question.objects.get(uuid=question_uuid)
        except models.Question.DoesNotExist:
            raise serializers.ValidationError(
                f"Question with UUID {question_uuid} does not exist"
            )

        # Get completion from context
        completion = self.context.get("completion")
        if not completion:
            raise serializers.ValidationError("Completion context is required")

        # Validate question belongs to the completion's checklist
        if question.checklist != completion.checklist:
            raise serializers.ValidationError(
                f"Question {question_uuid} does not belong to this checklist"
            )

        # Validate answer data for question type (skip validation for null values - they indicate removal)
        if answer_data is not None and not question.is_valid_answer(answer_data):
            raise serializers.ValidationError(
                f"Answer value '{answer_data}' is not valid for the question '{question}' (type: {question.question_type})."
            )

        attrs["question"] = question
        return attrs


class ChecklistCompletionSerializer(serializers.ModelSerializer):
    """Generic serializer for checklist completion status (basic view - no review triggers)."""

    completion_percentage = serializers.SerializerMethodField()
    unanswered_required_questions = serializers.SerializerMethodField()
    checklist_name = serializers.CharField(source="checklist.name", read_only=True)
    checklist_description = serializers.CharField(
        source="checklist.description", read_only=True
    )

    class Meta:
        model = models.ChecklistCompletion
        fields = (
            "uuid",
            "is_completed",
            "completion_percentage",
            "unanswered_required_questions",
            "checklist_name",
            "checklist_description",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "is_completed",
            "completion_percentage",
            "unanswered_required_questions",
            "checklist_name",
            "checklist_description",
            "created",
            "modified",
        )

    @extend_schema_field(serializers.FloatField())
    def get_completion_percentage(self, obj):
        return obj.get_completion_percentage()

    @extend_schema_field(serializers.ListField())
    def get_unanswered_required_questions(self, obj):
        unanswered = obj.get_unanswered_required_questions()
        return [
            {
                "uuid": str(q.uuid),
                "description": q.description,
                "question_type": q.question_type,
            }
            for q in unanswered
        ]


class ChecklistCompletionReviewerSerializer(ChecklistCompletionSerializer):
    """Extended serializer for checklist completion with review information (reviewer view)."""

    review_trigger_summary = serializers.SerializerMethodField()
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.full_name", read_only=True
    )

    class Meta(ChecklistCompletionSerializer.Meta):
        fields = ChecklistCompletionSerializer.Meta.fields + (
            "requires_review",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
            "review_trigger_summary",
        )
        read_only_fields = ChecklistCompletionSerializer.Meta.read_only_fields + (
            "requires_review",
            "review_trigger_summary",
        )

    @extend_schema_field(serializers.ListField())
    def get_review_trigger_summary(self, obj):
        return obj.get_review_trigger_summary()


class AnswerSubmitResponseSerializer(serializers.Serializer):
    """Generic response serializer for answer submission."""

    detail = serializers.CharField()
    completion = ChecklistCompletionSerializer()


class ChecklistShortSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField()
    checklist_type = serializers.CharField()


class ChecklistResponseSerializer(serializers.Serializer):
    """Generic response serializer for checklist with questions and completion (basic view)."""

    checklist = serializers.SerializerMethodField()
    completion = ChecklistCompletionSerializer()
    questions = QuestionWithAnswerSerializer(many=True)

    @extend_schema_field(ChecklistShortSerializer)
    def get_checklist(self, obj):
        """Get checklist basic information."""
        return {
            "uuid": str(obj["checklist"].uuid),
            "name": obj["checklist"].name,
            "description": obj["checklist"].description,
            "checklist_type": obj["checklist"].checklist_type,
        }


class ChecklistReviewerResponseSerializer(serializers.Serializer):
    """Generic response serializer for checklist with full review information (reviewer view)."""

    checklist = serializers.SerializerMethodField()
    completion = ChecklistCompletionReviewerSerializer()
    questions = QuestionWithAnswerReviewerSerializer(many=True)

    @extend_schema_field(ChecklistShortSerializer)
    def get_checklist(self, obj):
        """Get checklist basic information."""
        return {
            "uuid": str(obj["checklist"].uuid),
            "name": obj["checklist"].name,
            "description": obj["checklist"].description,
            "checklist_type": obj["checklist"].checklist_type,
        }


class ChecklistTemplateSerializer(serializers.Serializer):
    """Serializer for checklist template used when creating new objects."""

    checklist = serializers.SerializerMethodField()
    questions = QuestionSerializer(many=True)
    initial_visible_questions = QuestionSerializer(many=True)

    @extend_schema_field(ChecklistShortSerializer)
    def get_checklist(self, obj):
        """Get checklist basic information."""
        return {
            "uuid": str(obj["checklist"].uuid),
            "name": obj["checklist"].name,
            "description": obj["checklist"].description,
            "checklist_type": obj["checklist"].checklist_type,
        }
