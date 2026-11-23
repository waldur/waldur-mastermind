from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.utils import serialize_completion_answers
from waldur_core.core import serializers as core_serializers
from waldur_core.structure.serializers import ProjectMetadataAnswerSerializer
from waldur_mastermind.notifications import serializers as notifications_serializers

from . import models
from .questions import (
    SurveyQuestionSetField,
    create_question_set,
    replace_question_set,
    validate_question_set,
)
from .utils import QUESTIONNAIRE_LOCKED


class FeedbackSurveySerializer(
    core_serializers.AugmentedSerializerMixin, serializers.ModelSerializer
):
    audience = notifications_serializers.QueryOutputSerializer(read_only=True)
    audience_query = notifications_serializers.BroadcastMessageQuerySerializer(
        write_only=True
    )
    is_open = serializers.ReadOnlyField()

    class Meta:
        model = models.FeedbackSurvey
        fields = (
            "uuid",
            "created",
            "name",
            "intro",
            "starts_at",
            "ends_at",
            "is_active",
            "is_open",
            "audience",
            "audience_query",
            "max_prompts",
            "snooze_days",
        )
        read_only_fields = ("uuid", "created")

    def validate_audience_query(self, query):
        if not (
            query.get("all_users") or query.get("customers") or query.get("offerings")
        ):
            raise serializers.ValidationError(
                "Choose all users, organizations or offerings."
            )
        return query

    def validate(self, attrs):
        starts_at = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts_at and ends_at and starts_at >= ends_at:
            raise serializers.ValidationError({"ends_at": "End must be after start."})
        if "audience_query" in attrs:
            attrs["audience"] = notifications_serializers.serialize_query(
                attrs.pop("audience_query")
            )
        return attrs


class FeedbackSurveyDetailSerializer(FeedbackSurveySerializer):
    questions = SurveyQuestionSetField(required=False)

    class Meta(FeedbackSurveySerializer.Meta):
        fields = FeedbackSurveySerializer.Meta.fields + ("questions",)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance:
            # Homeport leaves the questions out once the survey is locked, so
            # any value here is an attempt to change them.
            if "questions" in attrs and self.instance.requests.exists():
                raise serializers.ValidationError({"questions": [QUESTIONNAIRE_LOCKED]})
        elif "questions" not in attrs:
            # Required on create only: the field stays optional in the schema
            # because create and update share it, and an update may leave the
            # questions out.
            raise serializers.ValidationError(
                {"questions": [serializers.Field.default_error_messages["required"]]}
            )
        if "questions" in attrs:
            validate_question_set(attrs["questions"])
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        # Each survey owns its checklist, so editing its questions never
        # reaches another survey.
        validated_data["checklist"] = self._create_checklist(
            validated_data["name"], validated_data.pop("questions")
        )
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        question_set = validated_data.pop("questions", None)
        if question_set is not None:
            # A prompt inserting a request must wait for this row lock, so no
            # one is prompted between the check and the new questions.
            locked = models.FeedbackSurvey.objects.select_for_update().get(
                pk=instance.pk
            )
            if models.FeedbackRequest.objects.filter(survey=locked).exists():
                raise serializers.ValidationError({"questions": [QUESTIONNAIRE_LOCKED]})
            replace_question_set(instance.checklist, question_set)
        return super().update(instance, validated_data)

    def _create_checklist(self, name, question_set):
        checklist = checklist_models.Checklist.objects.create(
            name=name, checklist_type=checklist_enums.ChecklistTypes.FEEDBACK
        )
        create_question_set(checklist, question_set)
        return checklist


class FeedbackSurveyPublicSerializer(serializers.ModelSerializer):
    """What a prompted user sees of a survey."""

    class Meta:
        model = models.FeedbackSurvey
        fields = ("uuid", "name", "intro", "ends_at")


class FeedbackPromptSerializer(serializers.Serializer):
    request_uuid = serializers.UUIDField(source="uuid")
    survey = FeedbackSurveyPublicSerializer()


class FeedbackRequestSerializer(serializers.ModelSerializer):
    survey_uuid = serializers.UUIDField(source="survey.uuid", read_only=True)
    survey_name = serializers.ReadOnlyField(source="survey.name")
    user_uuid = serializers.UUIDField(source="user.uuid", read_only=True)
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_email = serializers.ReadOnlyField(source="user.email")
    organization_names = serializers.ListField(
        child=serializers.CharField(), read_only=True
    )
    answers = serializers.SerializerMethodField()

    class Meta:
        model = models.FeedbackRequest
        fields = (
            "uuid",
            "created",
            "survey_uuid",
            "survey_name",
            "user_uuid",
            "user_full_name",
            "user_username",
            "user_email",
            "organization_names",
            "state",
            "prompt_count",
            "snoozed_until",
            "answered_at",
            "answers",
        )
        read_only_fields = fields

    @extend_schema_field(ProjectMetadataAnswerSerializer(many=True))
    def get_answers(self, request) -> list[dict]:
        completion = request.checklist_completion
        return serialize_completion_answers(completion) if completion else []


class FeedbackDistributionItemSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()
    count = serializers.IntegerField()


class FeedbackQuestionSummarySerializer(serializers.Serializer):
    question_uuid = serializers.CharField()
    question = serializers.CharField()
    question_type = serializers.CharField()
    answer_count = serializers.IntegerField()
    mean = serializers.FloatField(allow_null=True)
    distribution = FeedbackDistributionItemSerializer(many=True)


class FeedbackSurveySummarySerializer(serializers.Serializer):
    prompted = serializers.IntegerField()
    answered = serializers.IntegerField()
    dismissed = serializers.IntegerField()
    pending = serializers.IntegerField()
    expired = serializers.IntegerField()
    questions = FeedbackQuestionSummarySerializer(many=True)
