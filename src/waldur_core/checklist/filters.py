import django_filters

from . import models


class QuestionFilter(django_filters.FilterSet):
    checklist_uuid = django_filters.UUIDFilter(field_name="checklist__uuid")

    class Meta:
        model = models.Question
        fields = []


class QuestionOptionFilter(django_filters.FilterSet):
    question_uuid = django_filters.UUIDFilter(field_name="question__uuid")

    class Meta:
        model = models.QuestionOption
        fields = []


class QuestionDependencyFilter(django_filters.FilterSet):
    question_uuid = django_filters.UUIDFilter(field_name="question__uuid")
    depends_on_question_uuid = django_filters.UUIDFilter(
        field_name="depends_on_question__uuid"
    )

    class Meta:
        model = models.QuestionDependency
        fields = []
