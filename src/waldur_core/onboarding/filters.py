import django_filters

from . import models


class OnboardingCountryChecklistConfigurationFilter(django_filters.FilterSet):
    country = django_filters.CharFilter(field_name="country")
    is_active = django_filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = models.OnboardingCountryChecklistConfiguration
        fields = ["country", "is_active"]


class OnboardingQuestionMetadataFilter(django_filters.FilterSet):
    checklist_uuid = django_filters.UUIDFilter(
        field_name="question__checklist__uuid", label="Checklist uuid"
    )
    question_uuid = django_filters.UUIDFilter(field_name="question__uuid")
    maps_to_customer_field = django_filters.CharFilter(
        field_name="maps_to_customer_field"
    )
    intent_field = django_filters.CharFilter(field_name="intent_field")

    class Meta:
        model = models.OnboardingQuestionMetadata
        fields = ["maps_to_customer_field", "intent_field"]
