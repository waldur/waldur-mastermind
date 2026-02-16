import django_filters

from waldur_core.core import filters as core_filters

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
    checklist_uuid = core_filters.RelatedUUIDFilter(
        view_name="checklists-admin-detail", field_name="checklist__uuid"
    )
    checklist_type = django_filters.ChoiceFilter(
        field_name="checklist__checklist_type",
        choices=enums.ChecklistTypes.CHOICES,
    )
    has_onboarding_mapping = django_filters.BooleanFilter(
        help_text="Filter questions that have onboarding metadata mapping",
        method="filter_has_onboarding_mapping",
    )

    def filter_has_onboarding_mapping(self, queryset, name, value):
        """Filter questions based on whether they have onboarding metadata."""
        if value:
            return queryset.filter(onboarding_metadata__isnull=False)
        else:
            return queryset.filter(onboarding_metadata__isnull=True)

    class Meta:
        model = models.Question
        fields = []


class QuestionOptionFilter(django_filters.FilterSet):
    question_uuid = core_filters.RelatedUUIDFilter(
        view_name="checklists-admin-questions-detail", field_name="question__uuid"
    )

    class Meta:
        model = models.QuestionOption
        fields = []


class QuestionDependencyFilter(django_filters.FilterSet):
    question_uuid = core_filters.RelatedUUIDFilter(
        view_name="checklists-admin-questions-detail", field_name="question__uuid"
    )
    depends_on_question_uuid = core_filters.RelatedUUIDFilter(
        view_name="checklists-admin-questions-detail",
        field_name="depends_on_question__uuid",
    )

    class Meta:
        model = models.QuestionDependency
        fields = []
