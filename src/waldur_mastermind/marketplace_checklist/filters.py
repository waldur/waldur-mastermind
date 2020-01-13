import django_filters

from . import models


class AnswerFilter(django_filters.FilterSet):
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    question_uuid = django_filters.UUIDFilter(field_name='question__uuid')
    checklist_uuid = django_filters.UUIDFilter(field_name='question__checklist__uuid')

    class Meta:
        model = models.Answer
        fields = (
            'project_uuid',
            'question_uuid',
            'checklist_uuid',
        )
