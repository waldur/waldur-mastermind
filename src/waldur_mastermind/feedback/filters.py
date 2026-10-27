import django_filters

from waldur_core.core import filters as core_filters

from . import models


class FeedbackSurveyFilterSet(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            ("name", "name"),
            ("starts_at", "starts_at"),
            ("ends_at", "ends_at"),
        )
    )

    class Meta:
        model = models.FeedbackSurvey
        fields = ()


class FeedbackRequestFilterSet(django_filters.FilterSet):
    survey_uuid = core_filters.RelatedUUIDFilter(
        view_name="feedbacksurvey-detail", field_name="survey__uuid"
    )
    state = django_filters.MultipleChoiceFilter(
        choices=models.FeedbackRequest.States.CHOICES
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", method="filter_customer"
    )
    answered_after = django_filters.DateFilter(
        field_name="answered_at", lookup_expr="date__gte"
    )
    answered_before = django_filters.DateFilter(
        field_name="answered_at", lookup_expr="date__lte"
    )

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            ("created", "created"),
            ("answered_at", "answered_at"),
            ("prompt_count", "prompt_count"),
            ("state", "state"),
        )
    )

    class Meta:
        model = models.FeedbackRequest
        fields = ()

    def filter_customer(self, queryset, name, value):
        return queryset.filter(organization_uuids__contains=[value.hex])
