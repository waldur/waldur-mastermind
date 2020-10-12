from rest_framework import serializers

from waldur_core.media.serializers import ProtectedMediaSerializerMixin

from . import models


class CategorySerializer(
    ProtectedMediaSerializerMixin, serializers.HyperlinkedModelSerializer
):
    checklists_count = serializers.ReadOnlyField(source='checklists.count')

    class Meta:
        model = models.Category
        fields = ('uuid', 'icon', 'url', 'name', 'description', 'checklists_count')
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-checklists-category-detail',
            },
        }


class ChecklistSerializer(serializers.ModelSerializer):
    questions_count = serializers.ReadOnlyField(source='questions.count')
    category_name = serializers.ReadOnlyField(source='category.name')
    category_uuid = serializers.ReadOnlyField(source='category.uuid')

    class Meta:
        model = models.Checklist
        fields = (
            'uuid',
            'name',
            'description',
            'questions_count',
            'category_name',
            'category_uuid',
        )


class QuestionSerializer(serializers.ModelSerializer):
    category_uuid = serializers.ReadOnlyField(source='category.uuid')

    class Meta:
        model = models.Question
        fields = ('uuid', 'description', 'solution', 'category_uuid', 'correct_answer')


class AnswerListSerializer(serializers.ModelSerializer):
    question_uuid = serializers.ReadOnlyField(source='question.uuid')

    class Meta:
        model = models.Answer
        fields = ('question_uuid', 'value')


class AnswerSubmitSerializer(serializers.Serializer):
    question_uuid = serializers.UUIDField()
    value = serializers.NullBooleanField()


class CustomerChecklistUpdateSerializer(serializers.ListSerializer):
    child = serializers.SlugRelatedField(
        slug_field='uuid', write_only=True, queryset=models.Checklist.objects.all(),
    )
