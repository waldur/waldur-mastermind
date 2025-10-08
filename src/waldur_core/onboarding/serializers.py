from rest_framework import serializers

from . import enums
from .models import (
    OnboardingJustification,
    OnboardingJustificationDocumentation,
    OnboardingVerification,
)
from .validators import onboarding_validator


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

    class Meta:
        model = OnboardingVerification
        fields = [
            "uuid",
            "user",
            "country",
            "legal_person_identifier",
            "legal_name",
            "user_submitted_customer_metadata",
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
            "created",
            "modified",
        ]


class OnboardingCompanyValidationRequestSerializer(serializers.Serializer):
    """Serializer for company validation requests."""

    country = serializers.CharField(
        max_length=2, help_text="ISO country code (e.g., 'EE' for Estonia)"
    )
    legal_person_identifier = serializers.CharField(
        max_length=50, help_text="Official company registration code"
    )
    legal_name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text="Company name (optional, for reference)",
    )
    user_submitted_customer_metadata = serializers.JSONField(
        required=False,
        default=dict,
        help_text="Optional customer metadata for manual verification cases. Should contain valid Customer model fields.",
    )

    def validate_country(self, value):
        """Validate that the country is supported."""
        supported_countries = onboarding_validator.get_supported_countries()
        if value not in supported_countries:
            raise serializers.ValidationError(
                f"Country '{value}' is not supported. Supported countries: {', '.join(supported_countries)}"
            )
        return value


class OnboardingCreateCustomerRequestSerializer(serializers.Serializer):
    """Serializer for creating customer from verification."""

    verification_uuid = serializers.UUIDField(
        help_text="UUID of the OnboardingVerification to create customer from"
    )


class OnboardingJustificationSerializer(serializers.ModelSerializer):
    """Serializer for OnboardingJustification model."""

    supporting_documentation = OnboardingJustificationDocumentationSerializer(
        many=True, read_only=True
    )

    class Meta:
        model = OnboardingJustification
        fields = [
            "uuid",
            "verification",
            "user",
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
        help_text="User's explanation for why they should be authorized"
    )

    def validate_verification_uuid(self, value):
        """Validate that the verification exists and is failed."""
        try:
            verification = OnboardingVerification.objects.get(uuid=value)
        except OnboardingVerification.DoesNotExist:
            raise serializers.ValidationError("Verification not found")

        if not verification.status == enums.VerificationStatus.FAILED:
            raise serializers.ValidationError("Can only justify failed verifications")

        return value
