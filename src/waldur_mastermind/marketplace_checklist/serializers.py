from rest_framework import serializers

from . import models


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


class ChecklistSerializer(serializers.ModelSerializer):
    questions_count = serializers.IntegerField(source="questions.count", read_only=True)
    category_name = serializers.ReadOnlyField(source="category.name")
    category_uuid = serializers.UUIDField(read_only=True, source="category.uuid")
    roles = serializers.SerializerMethodField()

    def get_roles(self, checklist) -> list[str]:
        return checklist.roles.values_list("name", flat=True)

    class Meta:
        model = models.Checklist
        fields = (
            "uuid",
            "name",
            "description",
            "questions_count",
            "category_name",
            "category_uuid",
            "roles",
        )


class ChecklistQuestionSerializer(serializers.ModelSerializer):
    category_uuid = serializers.UUIDField(read_only=True, source="category.uuid")

    class Meta:
        model = models.Question
        fields = (
            "uuid",
            "description",
            "solution",
            "category_uuid",
            "correct_answer",
            "image",
        )


class ImportExportQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Question
        fields = ("id", "description", "solution", "correct_answer", "order")


class AnswerListSerializer(serializers.ModelSerializer):
    question_uuid = serializers.UUIDField(read_only=True, source="question.uuid")

    class Meta:
        model = models.Answer
        fields = ("question_uuid", "value")


class AnswerSubmitSerializer(serializers.Serializer):
    question_uuid = serializers.UUIDField()
    value = serializers.BooleanField(allow_null=True)


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
