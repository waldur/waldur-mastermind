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
                "view_name": "marketplace-checklists-category-detail",
            },
        }


class ChecklistSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    questions_count = serializers.IntegerField(source="questions.count", read_only=True)
    category_name = serializers.ReadOnlyField(source="category.name")
    category_uuid = serializers.UUIDField(read_only=True, source="category.uuid")
    roles = serializers.SerializerMethodField()

    def get_roles(self, checklist) -> list[str]:
        return checklist.roles.values_list("name", flat=True)

    class Meta:
        model = models.Checklist
        view_name = "marketplace-checklist-detail"
        fields = [
            "uuid",
            "url",
            "name",
            "description",
            "questions_count",
            "category_name",
            "category_uuid",
            "roles",
        ]

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }


class ChecklistAdminSerializer(ChecklistSerializer):
    checklist_type = serializers.CharField(
        source="get_checklist_type_display", read_only=True
    )

    class Meta(ChecklistSerializer.Meta):
        fields = ChecklistSerializer.Meta.fields + ["checklist_type"]
        view_name = "marketplace-checklists-admin-detail"


class CreateChecklistSerializer(ChecklistAdminSerializer):
    checklist_type = serializers.ChoiceField(choices=enums.ChecklistTypes.CHOICES)
    roles = serializers.HyperlinkedRelatedField(
        queryset=models.Role.objects.all(),
        many=True,
        required=False,
        view_name="role-detail",
        lookup_field="uuid",
    )

    def create(self, validated_data):
        roles = validated_data.pop("roles", [])
        checklist = super().create(validated_data)
        if roles:
            checklist.roles.set(roles)
        return checklist

    def update(self, instance, validated_data):
        roles = validated_data.pop("roles", None)
        checklist = super().update(instance, validated_data)
        if roles is not None:
            checklist.roles.set(roles)
        return checklist


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
        view_name="marketplace-checklists-admin-question-detail",
        lookup_field="uuid",
    )
    question_uuid = serializers.UUIDField(source="question.uuid", read_only=True)

    class Meta(QuestionOptionsSerializer.Meta):
        view_name = "marketplace-checklists-admin-question-option-detail"
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
        view_name="marketplace-checklists-admin-question-detail",
        lookup_field="uuid",
    )
    question_name = serializers.CharField(source="question.description", read_only=True)
    depends_on_question = serializers.HyperlinkedRelatedField(
        queryset=models.Question.objects.all(),
        required=True,
        view_name="marketplace-checklists-admin-question-detail",
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
        view_name = "marketplace-checklists-admin-question-dependency-detail"
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
            "image",
            "question_options",
        ]


class QuestionAdminSerializer(QuestionSerializer):
    question_options = QuestionOptionsAdminSerializer(many=True, read_only=True)
    checklist = serializers.HyperlinkedRelatedField(
        queryset=models.Checklist.objects.all(),
        required=True,
        view_name="marketplace-checklists-admin-detail",
        lookup_field="uuid",
    )
    checklist_name = serializers.UUIDField(read_only=True, source="checklist.name")
    checklist_uuid = serializers.UUIDField(read_only=True, source="checklist.uuid")

    def validate(self, attrs):
        operator = attrs.get("operator")
        review_answer_value = attrs.get("review_answer_value")
        question_type = attrs.get("question_type")

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

        return attrs

    class Meta(QuestionSerializer.Meta):
        view_name = "marketplace-checklists-admin-question-detail"
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
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }


class ImportExportQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Question
        fields = ("id", "description", "order")


class AnswerListSerializer(serializers.ModelSerializer):
    question_uuid = serializers.UUIDField(read_only=True, source="question.uuid")

    class Meta:
        model = models.Answer
        fields = ("question_uuid", "answer_data")


class AnswerSubmitSerializer(serializers.Serializer):
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

        # Validate answer data for question type
        if not question.is_valid_answer(answer_data):
            raise serializers.ValidationError(
                f"Answer value '{answer_data}' is not valid for the question '{question}' (type: {question.question_type})."
            )

        return attrs


class CustomerChecklistUpdateSerializer(serializers.ListSerializer):
    child = serializers.SlugRelatedField(
        slug_field="uuid",
        write_only=True,
        queryset=models.Checklist.objects.all(),
    )


class CustomerChecklistStatSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    uuid = serializers.CharField(read_only=True)
    score = serializers.FloatField(read_only=True)


class UserStatsSerializer(serializers.Serializer):
    score = serializers.FloatField(read_only=True)


class ProjectStatsItemSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    uuid = serializers.UUIDField(read_only=True)
    positive_count = serializers.IntegerField(read_only=True)
    negative_count = serializers.IntegerField(read_only=True)
    unknown_count = serializers.IntegerField(read_only=True)
    score = serializers.FloatField(read_only=True)


class ChecklistProjectStatsSerializer(serializers.ListSerializer):
    child = ProjectStatsItemSerializer()


class ChecklistCustomerStatsSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    uuid = serializers.UUIDField(read_only=True)
    latitude = serializers.FloatField(read_only=True)
    longitude = serializers.FloatField(read_only=True)
    score = serializers.FloatField(read_only=True)
