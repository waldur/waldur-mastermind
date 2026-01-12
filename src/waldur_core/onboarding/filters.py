import django_filters

from . import models


class OnboardingVerificationFilter(django_filters.FilterSet):
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid", label="User UUID")
    country = django_filters.CharFilter(field_name="country")
    legal_name = django_filters.CharFilter(
        field_name="legal_name", lookup_expr="icontains"
    )
    legal_person_identifier = django_filters.CharFilter(
        field_name="legal_person_identifier", lookup_expr="icontains"
    )
    status = django_filters.CharFilter(field_name="status")

    class Meta:
        model = models.OnboardingVerification
        fields = [
            "user_uuid",
            "country",
            "legal_name",
            "legal_person_identifier",
            "status",
        ]


class OnboardingJustificationFilter(django_filters.FilterSet):
    verification_uuid = django_filters.UUIDFilter(
        field_name="verification__uuid", label="Verification UUID"
    )
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid", label="User UUID")

    class Meta:
        model = models.OnboardingJustification
        fields = ["verification_uuid", "user_uuid"]


class OnboardingQuestionMetadataFilter(django_filters.FilterSet):
    checklist_uuid = django_filters.UUIDFilter(
        field_name="question__checklist__uuid", label="Checklist uuid"
    )
    question_uuid = django_filters.UUIDFilter(field_name="question__uuid")
    question_description = django_filters.CharFilter(
        field_name="question__description", lookup_expr="icontains"
    )
    maps_to_customer_field = django_filters.CharFilter(
        field_name="maps_to_customer_field", lookup_expr="icontains"
    )
    intent_field = django_filters.CharFilter(
        field_name="intent_field", lookup_expr="icontains"
    )

    class Meta:
        model = models.OnboardingQuestionMetadata
        fields = ["maps_to_customer_field", "intent_field"]
