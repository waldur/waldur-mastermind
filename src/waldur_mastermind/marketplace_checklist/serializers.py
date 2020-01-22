from rest_framework import serializers

from . import models


class ChecklistSerializer(serializers.ModelSerializer):
    questions_count = serializers.ReadOnlyField(source='questions.count')

    class Meta:
        model = models.Checklist
        fields = ('uuid', 'name', 'description', 'questions_count')


class QuestionSerializer(serializers.ModelSerializer):
    category_uuid = serializers.ReadOnlyField(source='category.uuid')

    class Meta:
        model = models.Question
        fields = ('uuid', 'description', 'solution', 'category_uuid')


class AnswerListSerializer(serializers.ModelSerializer):
    question_uuid = serializers.ReadOnlyField(source='question.uuid')

    class Meta:
        model = models.Answer
        fields = ('question_uuid', 'value')


class AnswerSubmitSerializer(serializers.Serializer):
    question_uuid = serializers.UUIDField()
    value = serializers.NullBooleanField()
