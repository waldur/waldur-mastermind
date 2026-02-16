import django_filters
from django.db.models import Q

from waldur_core.core import filters as core_filters

from . import enums, models


class OnboardingVerificationFilter(django_filters.FilterSet):
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid", label="User UUID"
    )
    country = django_filters.CharFilter(field_name="country")
    legal_name = django_filters.CharFilter(
        field_name="legal_name", lookup_expr="icontains"
    )
    legal_person_identifier = django_filters.CharFilter(
        field_name="legal_person_identifier", lookup_expr="icontains"
    )
    status = core_filters.MappedMultipleChoiceFilter(
        enums.VerificationStatus.CHOICES,
        label="Verification status",
    )
    validation_method = core_filters.MappedMultipleChoiceFilter(
        enums.ValidationMethod.CHOICES,
        label="Validation method",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Filter by legal name, legal person identifier",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("created", "created"),
            ("modified", "modified"),
            ("validated_at", "validated_at"),
            ("expires_at", "expires_at"),
        )
    )

    class Meta:
        model = models.OnboardingVerification
        fields = [
            "user_uuid",
            "country",
            "legal_name",
            "legal_person_identifier",
            "validation_method",
            "status",
        ]

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(legal_name__icontains=value)
            | Q(verified_company_data__name__icontains=value)
            | Q(legal_person_identifier__icontains=value)
        ).distinct()


class OnboardingJustificationFilter(django_filters.FilterSet):
    verification_uuid = core_filters.RelatedUUIDFilter(
        view_name="onboarding-verification-detail",
        field_name="verification__uuid",
        label="Verification UUID",
    )
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid", label="User UUID"
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Filter by legal name, legal person identifier",
    )
    validation_decision = core_filters.MappedMultipleChoiceFilter(
        enums.ReviewDecision.CHOICES,
        label="Review decision",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("created", "created"),
            ("modified", "modified"),
            ("validated_at", "validated_at"),
        )
    )

    class Meta:
        model = models.OnboardingJustification
        fields = ["verification_uuid", "user_uuid", "validation_decision"]

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(verification__legal_name__icontains=value)
            | Q(verification__verified_company_data__name__icontains=value)
            | Q(verification__legal_person_identifier__icontains=value)
        ).distinct()


class OnboardingQuestionMetadataFilter(django_filters.FilterSet):
    checklist_uuid = core_filters.RelatedUUIDFilter(
        view_name="checklists-admin-detail",
        field_name="question__checklist__uuid",
        label="Checklist uuid",
    )
    question_uuid = core_filters.RelatedUUIDFilter(
        view_name="checklists-admin-questions-detail", field_name="question__uuid"
    )
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
