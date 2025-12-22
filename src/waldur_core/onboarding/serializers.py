from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.structure.models import Customer

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
    questions = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = OnboardingCountryChecklistConfiguration
        fields = [
            "url",
            "uuid",
            "country",
            "checklist",
            "checklist_name",
            "checklist_uuid",
            "questions",
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

    @extend_schema_field(checklist_serializers.QuestionAdminSerializer(many=True))
    def get_questions(self, obj):
        questions = obj.checklist.questions.all()
        return checklist_serializers.QuestionAdminSerializer(
            questions, many=True, context=self.context
        ).data


class OnboardingQuestionMetadataSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for OnboardingQuestionMetadata model."""

    question = serializers.HyperlinkedRelatedField(
        queryset=checklist_models.Question.objects.all(),
        lookup_field="uuid",
        view_name="checklists-admin-questions-detail",
    )
    question_description = serializers.CharField(
        source="question.description", read_only=True
    )
    question_uuid = serializers.UUIDField(source="question.uuid", read_only=True)
    checklist_name = serializers.CharField(
        source="question.checklist.name", read_only=True
    )

    class Meta:
        model = OnboardingQuestionMetadata
        fields = [
            "uuid",
            "url",
            "checklist_name",
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
        }
        read_only_fields = ["uuid", "created", "modified"]

    def validate_maps_to_customer_field(self, value):
        """
        Validate that the field name is a valid Customer model field
        """
        if not value:
            return value

        customer_field_names = [field.name for field in Customer._meta.get_fields()]

        if value not in customer_field_names:
            raise serializers.ValidationError(
                f"'{value}' is not a valid Customer model field. "
                f"Available fields: {', '.join(sorted(customer_field_names))}"
            )

        return value


class OnboardingJustificationDocumentationSerializer(serializers.ModelSerializer):
    """Serializer for OnboardingJustificationDocumentation model."""

    file_name = serializers.CharField(source="file.name", read_only=True)
    file_size = serializers.IntegerField(source="file.size", read_only=True)

    class Meta:
        model = OnboardingJustificationDocumentation
        fields = ["uuid", "file", "file_name", "file_size", "created"]
        read_only_fields = ["uuid", "created"]


class OnboardingJustificationSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for OnboardingJustification model."""

    verification_uuid = serializers.UUIDField(
        source="verification.uuid", read_only=True
    )
    legal_person_identifier = serializers.CharField(
        source="verification.legal_person_identifier", read_only=True
    )
    legal_name = serializers.CharField(source="verification.legal_name", read_only=True)
    country = serializers.CharField(source="verification.country", read_only=True)
    supporting_documentation = OnboardingJustificationDocumentationSerializer(
        many=True, read_only=True
    )
    user_full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    error_message = serializers.CharField(
        source="verification.error_message", read_only=True
    )
    error_traceback = serializers.CharField(
        source="verification.error_traceback", read_only=True
    )
    onboarding_metadata = serializers.SerializerMethodField(
        help_text="Onboarding-specific data like intents, purposes extracted from checklist answers"
    )
    user_submitted_customer_data = serializers.SerializerMethodField(
        help_text="Customer-related data submitted by the user via checklist answers"
    )

    class Meta:
        model = OnboardingJustification
        fields = [
            "uuid",
            "verification",
            "verification_uuid",
            "country",
            "user",
            "user_full_name",
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
            "onboarding_metadata",
            "user_submitted_customer_data",
            "created",
            "modified",
        ]
        read_only_fields = [
            "uuid",
            "user_full_name",
            "validated_by",
            "validated_at",
            "validation_decision",
            "staff_notes",
            "supporting_documentation",
            "onboarding_metadata",
            "user_submitted_customer_data",
            "created",
            "modified",
        ]
        extra_kwargs = {
            "verification": {
                "lookup_field": "uuid",
                "view_name": "onboarding-verification-detail",
            },
            "user": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
            "validated_by": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
        }

    def get_onboarding_metadata(self, obj) -> dict:
        """Get onboarding-specific metadata with human-readable labels."""
        return obj.verification.get_onboarding_metadata_display()

    def get_user_submitted_customer_data(self, obj) -> dict:
        """Get customer data submitted by the user during onboarding."""
        return obj.verification.get_user_submitted_customer_data()


class OnboardingVerificationSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for OnboardingVerification model."""

    onboarding_metadata = serializers.SerializerMethodField(
        help_text="Onboarding-specific data like intents, purposes extracted from checklist answers"
    )
    user_full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    user_submitted_customer_data = serializers.SerializerMethodField()
    can_customer_be_created = serializers.SerializerMethodField(
        help_text="Boolean indicating if a customer can be created from this verification"
    )
    customer_creation_error_message = serializers.SerializerMethodField(
        help_text="Reason why customer cannot be created (null if can be created)"
    )
    justifications = OnboardingJustificationSerializer(many=True, read_only=True)

    class Meta:
        model = OnboardingVerification
        fields = [
            "uuid",
            "user",
            "user_full_name",
            "country",
            "legal_person_identifier",
            "legal_name",
            "status",
            "justifications",
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
            "can_customer_be_created",
            "customer_creation_error_message",
            "created",
            "modified",
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "onboarding-verification-detail",
            },
            "user": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
            "customer": {
                "view_name": "customer-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
        }
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
            "can_customer_be_created",
            "customer_creation_error_message",
            "created",
            "modified",
        ]

    def get_onboarding_metadata(self, obj) -> dict:
        """Get onboarding-specific metadata with human-readable labels (UUIDs resolved to labels)."""
        return obj.get_onboarding_metadata_display()

    def get_user_submitted_customer_data(self, obj) -> dict:
        """Get customer data submitted by the user during onboarding."""
        return obj.get_user_submitted_customer_data()

    def get_can_customer_be_created(self, obj) -> bool:
        """Check if a customer can be created from this verification."""
        can_create, _ = obj.can_customer_be_created()
        return can_create

    def get_customer_creation_error_message(self, obj) -> str | None:
        """Get the error message explaining why a customer cannot be created."""
        can_create, error_message = obj.can_customer_be_created()
        return error_message if not can_create else None


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
    is_manual_validation = serializers.BooleanField(
        default=False,
        help_text="Indicates if the validation is to be performed manually",
    )


class OnboardingRunValidationRequestSerializer(serializers.Serializer):
    """
    Serializer for run_validation action request body.
    Accepts optional user identity fields as temporary workaround.
    """

    # ToDo: remove this after implementing getting user's identifier via auth methods
    person_identifier = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        help_text="Personal identifier (temporary workaround for Estonian civil_number)",
    )
    first_name = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        help_text="User's first name (temporary workaround for Austrian validation)",
    )
    last_name = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        help_text="User's last name (temporary workaround for Austrian validation)",
    )
    birth_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="User's birth date (temporary workaround for Austrian validation)",
    )


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
