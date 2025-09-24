import django_filters

from . import enums, models


class ChecklistFilter(django_filters.FilterSet):
    checklist_type = django_filters.ChoiceFilter(
        field_name="checklist_type",
        choices=enums.ChecklistTypes.CHOICES,
    )
    checklist_type__in = django_filters.MultipleChoiceFilter(
        field_name="checklist_type",
        choices=enums.ChecklistTypes.CHOICES,
        help_text="Filter by multiple checklist types",
    )

    class Meta:
        model = models.Checklist
        fields = ["checklist_type"]


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
