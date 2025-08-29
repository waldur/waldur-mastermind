from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers

from . import enums, models, utils


class ChecklistCategorySerializer(serializers.HyperlinkedModelSerializer):
    checklists_count = serializers.IntegerField(
        source="checklists.count", read_only=True
    )

    class Meta:
        model = models.Category
        fields = ("uuid", "icon", "url", "name", "description", "checklists_count")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "checklists-admin-categories-detail",
            },
        }


class ChecklistSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    questions_count = serializers.IntegerField(source="questions.count", read_only=True)
    category_name = serializers.ReadOnlyField(source="category.name")
    category_uuid = serializers.UUIDField(source="category.uuid", read_only=True)
    category = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.Category.objects.all(),
        required=False,
        allow_null=True,
        help_text="Category of the checklist",
    )

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
            "category_name",
            "category_uuid",
            "category",
        ]

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }


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
    question_options = QuestionOptionsSerializer(many=True, read_only=True)

    class Meta:
        model = models.Question
        fields = [
            "uuid",
            "description",
            "user_guidance",
            "question_options",
        ]


class QuestionWithAnswerSerializer(serializers.ModelSerializer):
    """Generic serializer for questions with existing answer context (basic view - no review logic)."""

    existing_answer = serializers.SerializerMethodField()
    question_options = serializers.SerializerMethodField()
    user_guidance = serializers.SerializerMethodField()

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
        )

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_existing_answer(self, obj):
        """Get existing answer for this question in the current completion context."""
        request = self.context.get("request")
        completion = self.context.get("completion")

        if not request or not completion:
            return None

        try:
            answer = completion.answers.get(question=obj, user=request.user)
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


class QuestionWithAnswerReviewerSerializer(QuestionWithAnswerSerializer):
    """Extended serializer for questions with review logic (reviewer view)."""

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

        # Validate min/max values for NUMBER questions only
        if question_type and question_type != "number":
            if min_value is not None or max_value is not None:
                raise serializers.ValidationError(
                    "Min and max values can only be set for NUMBER type questions."
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

        return attrs

    class Meta(QuestionSerializer.Meta):
        view_name = "checklists-admin-questions-detail"
        fields = QuestionSerializer.Meta.fields + [
            "url",
            "checklist_name",
            "checklist_uuid",
            "checklist",
            "order",
            "required",
            "question_type",
            "question_options",
            "operator",
            "review_answer_value",
            "always_requires_review",
            "guidance_answer_value",
            "guidance_operator",
            "always_show_guidance",
            "min_value",
            "max_value",
            "dependency_logic_operator",
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }


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


class AnswerSubmitSerializer(serializers.Serializer):
    """Generic serializer for submitting checklist answers."""

    question_uuid = serializers.UUIDField()
    answer_data = serializers.JSONField(allow_null=True)

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


class ChecklistResponseSerializer(serializers.Serializer):
    """Generic response serializer for checklist with questions and completion (basic view)."""

    checklist = serializers.SerializerMethodField()
    completion = ChecklistCompletionSerializer()
    questions = QuestionWithAnswerSerializer(many=True)

    @extend_schema_field(serializers.DictField())
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

    @extend_schema_field(serializers.DictField())
    def get_checklist(self, obj):
        """Get checklist basic information."""
        return {
            "uuid": str(obj["checklist"].uuid),
            "name": obj["checklist"].name,
            "description": obj["checklist"].description,
            "checklist_type": obj["checklist"].checklist_type,
        }
