from rest_framework import serializers

from . import models


class QuestionSerializer(serializers.ModelSerializer):
    category_uuid = serializers.ReadOnlyField(source='category.uuid')

    class Meta:
        model = models.Question
        fields = ('description', 'category_uuid')


class ChecklistSerializer(serializers.ModelSerializer):
    questions = QuestionSerializer(many=True, read_only=True)

    class Meta:
        model = models.Checklist
        fields = ('uuid', 'name', 'description', 'questions')


class AnswerSerializer(serializers.ModelSerializer):
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    question_uuid = serializers.ReadOnlyField(source='question.uuid')

    class Meta:
        model = models.Answer
        fields = ('project_uuid', 'question_uuid', 'value')
