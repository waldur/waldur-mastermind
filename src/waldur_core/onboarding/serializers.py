from rest_framework import serializers

from . import enums
from .models import (
    OnboardingCountryChecklistConfiguration,
    OnboardingJustification,
    OnboardingJustificationDocumentation,
    OnboardingQuestionMetadata,
    OnboardingVerification,
)


class OnboardingCountryChecklistConfigurationSerializer(
    serializers.HyperlinkedModelSerializer
):
    """Serializer for CountryChecklistConfiguration model."""

    checklist_name = serializers.CharField(source="checklist.name", read_only=True)
    checklist_uuid = serializers.UUIDField(source="checklist.uuid", read_only=True)

    class Meta:
        model = OnboardingCountryChecklistConfiguration
        fields = [
            "url",
            "uuid",
            "country",
            "checklist",
            "checklist_name",
            "checklist_uuid",
            "is_active",
            "created",
            "modified",
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "onboarding-country-config-detail",
            },
            "checklist": {
                "lookup_field": "uuid",
                "view_name": "checklists-admin-detail",
            },
        }
        read_only_fields = ["uuid", "created", "modified"]


class OnboardingQuestionMetadataSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for OnboardingQuestionMetadata model."""

    question_description = serializers.CharField(
        source="question.description", read_only=True
    )
    question_uuid = serializers.UUIDField(source="question.uuid", read_only=True)

    class Meta:
        model = OnboardingQuestionMetadata
        fields = [
            "uuid",
            "url",
            "question",
            "question_uuid",
            "question_description",
            "maps_to_customer_field",
            "intent_field",
            "created",
            "modified",
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "onboarding-question-metadata-detail",
            },
            "question": {
                "lookup_field": "uuid",
                "view_name": "checklists-admin-questions-detail",
            },
        }
        read_only_fields = ["uuid", "created", "modified"]


class OnboardingJustificationDocumentationSerializer(serializers.ModelSerializer):
    """Serializer for OnboardingJustificationDocumentation model."""

    file_name = serializers.CharField(source="file.name", read_only=True)
    file_size = serializers.IntegerField(source="file.size", read_only=True)

    class Meta:
        model = OnboardingJustificationDocumentation
        fields = ["uuid", "file", "file_name", "file_size", "created"]
        read_only_fields = ["uuid", "created"]


class OnboardingVerificationSerializer(serializers.ModelSerializer):
    """Serializer for OnboardingVerification model."""

    onboarding_metadata = serializers.SerializerMethodField(
        help_text="Onboarding-specific data like intents, purposes extracted from checklist answers"
    )
    user_submitted_customer_data = serializers.SerializerMethodField()

    class Meta:
        model = OnboardingVerification
        fields = [
            "uuid",
            "user",
            "country",
            "legal_person_identifier",
            "legal_name",
            "status",
            "validation_method",
            "verified_user_roles",
            "verified_company_data",
            "raw_response",
            "error_traceback",
            "error_message",
            "validated_at",
            "expires_at",
            "customer",
            "onboarding_metadata",
            "user_submitted_customer_data",
            "created",
            "modified",
        ]
        read_only_fields = [
            "uuid",
            "status",
            "validation_method",
            "verified_user_roles",
            "verified_company_data",
            "raw_response",
            "error_traceback",
            "error_message",
            "validated_at",
            "customer",
            "onboarding_metadata",
            "user_submitted_customer_data",
            "created",
            "modified",
        ]

    def get_onboarding_metadata(self, obj) -> dict:
        """Get onboarding-specific metadata like intents, purposes from checklist answers."""
        return obj.get_onboarding_metadata()

    def get_user_submitted_customer_data(self, obj) -> dict:
        """Get customer data submitted by the user during onboarding."""
        return obj.get_user_submitted_customer_data()


class OnboardingCompanyValidationRequestSerializer(serializers.Serializer):
    """Serializer for company validation requests."""

    country = serializers.CharField(
        max_length=2, help_text="ISO country code (e.g., 'EE' for Estonia)"
    )
    legal_person_identifier = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        help_text="Official company registration code",
    )
    legal_name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text="Company name (optional)",
    )


class OnboardingJustificationSerializer(serializers.ModelSerializer):
    """Serializer for OnboardingJustification model."""

    legal_person_identifier = serializers.CharField(
        source="verification.legal_person_identifier", read_only=True
    )
    legal_name = serializers.CharField(source="verification.legal_name", read_only=True)
    country = serializers.CharField(source="verification.country", read_only=True)
    supporting_documentation = OnboardingJustificationDocumentationSerializer(
        many=True, read_only=True
    )
    error_message = serializers.CharField(
        source="verification.error_message", read_only=True
    )
    error_traceback = serializers.CharField(
        source="verification.error_traceback", read_only=True
    )

    class Meta:
        model = OnboardingJustification
        fields = [
            "uuid",
            "verification",
            "country",
            "user",
            "legal_person_identifier",
            "legal_name",
            "error_message",
            "error_traceback",
            "user_justification",
            "validated_by",
            "validated_at",
            "validation_decision",
            "staff_notes",
            "supporting_documentation",
            "created",
            "modified",
        ]
        read_only_fields = [
            "uuid",
            "validated_by",
            "validated_at",
            "validation_decision",
            "staff_notes",
            "supporting_documentation",
            "created",
            "modified",
        ]


class OnboardingJustificationCreateSerializer(serializers.Serializer):
    """Serializer for creating justifications."""

    verification_uuid = serializers.UUIDField(
        help_text="UUID of the OnboardingVerification to justify"
    )
    user_justification = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="User's explanation for why they should be authorized",
    )

    def validate_verification_uuid(self, value):
        """Validate that the verification exists and is failed/escalated."""
        try:
            verification = OnboardingVerification.objects.get(uuid=value)
        except OnboardingVerification.DoesNotExist:
            raise serializers.ValidationError("Verification not found")

        if verification.status not in [
            enums.VerificationStatus.FAILED,
            enums.VerificationStatus.ESCALATED,
        ]:
            raise serializers.ValidationError(
                "Can only justify failed or escalated verifications"
            )

        return value


class OnboardingJustificationReviewSerializer(serializers.Serializer):
    """Serializer for staff review of justifications."""

    staff_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Administrator notes about the review decision",
    )
