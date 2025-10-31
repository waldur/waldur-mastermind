from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import exceptions, permissions, serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from waldur_core.checklist.mixins import UserChecklistMixin
from waldur_core.core import filters as core_filters
from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.structure import serializers as structure_serializers

from . import filters
from .models import (
    OnboardingCountryChecklistConfiguration,
    OnboardingJustification,
    OnboardingQuestionMetadata,
    OnboardingVerification,
)
from .serializers import (
    OnboardingCompanyValidationRequestSerializer,
    OnboardingCountryChecklistConfigurationSerializer,
    OnboardingJustificationCreateSerializer,
    OnboardingJustificationDocumentationSerializer,
    OnboardingJustificationReviewSerializer,
    OnboardingJustificationSerializer,
    OnboardingQuestionMetadataSerializer,
    OnboardingVerificationSerializer,
)
from .validators import onboarding_validator


class OnboardingVerificationViewSet(UserChecklistMixin, core_views.ActionsViewSet):
    """
    ViewSet for managing company onboarding verifications.

    Supports automatic validation with required fields (legal_person_identifier, person_identifier).
    Optionally integrates with checklist system for flexible, country-specific additional data collection.
    """

    queryset = OnboardingVerification.objects.all()
    serializer_class = OnboardingVerificationSerializer
    lookup_field = "uuid"
    filter_backends = (core_filters.StaffOrUserFilter, DjangoFilterBackend)

    # Override later with correct permissions per action
    checklist_permissions = [permissions.IsAuthenticated]
    completion_status_permissions = [permissions.IsAuthenticated]
    submit_answers_permissions = [permissions.IsAuthenticated]

    def get_checklist_completion(self, obj):
        return obj.get_or_create_checklist_completion()

    def get_permissions(self):
        """Get permissions based on action."""
        # Map actions to their permission attributes
        permission_map = {
            "checklist": "checklist_permissions",
            "completion_status": "completion_status_permissions",
            "submit_answers": "submit_answers_permissions",
        }

        # If this is a checklist-related action, use its specific permissions
        if self.action in permission_map:
            permission_attr = permission_map[self.action]
            permission_classes = getattr(self, permission_attr, [])
            return [permission() for permission in permission_classes]

        # Otherwise, use default permissions from parent class
        return super().get_permissions()

    @extend_schema(
        description="Start company validation process by creating a verification record. "
        "If a checklist is configured for the country, use checklist endpoints to submit additional answers. "
        "Then call run_validation to perform automatic validation.",
        request=OnboardingCompanyValidationRequestSerializer,
        responses=OnboardingVerificationSerializer,
    )
    @action(detail=False, methods=["post"])
    def start_verification(self, request):
        """
        Start company validation process by creating a verification record.

        Creates OnboardingVerification with required fields for automatic validation.
        If a checklist is configured for the country, it will be created for additional data collection.
        User can then proceed to run_validation directly or submit checklist answers first.
        """
        serializer = OnboardingCompanyValidationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Create verification record
        verification = OnboardingVerification.objects.create(
            user=request.user,
            country=serializer.validated_data["country"],
            legal_person_identifier=serializer.validated_data.get(
                "legal_person_identifier", ""
            ),
            legal_name=serializer.validated_data.get("legal_name", ""),
        )

        # Create checklist completion if available (optional)
        # This allows collecting additional country-specific data
        verification.get_or_create_checklist_completion()
        # If no checklist is configured, user can still proceed with automatic validation

        # Return the verification result
        response_serializer = OnboardingVerificationSerializer(verification)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    start_verification_serializer_class = OnboardingCompanyValidationRequestSerializer

    @extend_schema(
        description="Run automatic validation using the required fields provided during verification creation. "
        "Checklist answers (if any) are only used for supplemental customer/intent data.",
        request=None,
        responses=OnboardingVerificationSerializer,
    )
    @action(detail=True, methods=["post"])
    def run_validation(self, request, uuid=None):
        """
        Run automatic validation using verification data.

        Uses the required fields (legal_person_identifier, legal_name) provided during verification creation.
        Checklist answers are only used for supplemental customer data, not for verification fields.
        Runs validation backend and updates verification status.
        """
        verification = self.get_object()

        verification = onboarding_validator.validate_company(
            user=request.user,
            country=verification.country,
            legal_person_identifier=verification.legal_person_identifier,
            legal_name=verification.legal_name,
            existing_verification=verification,
        )

        response_serializer = OnboardingVerificationSerializer(verification)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    run_validation_serializer_class = OnboardingVerificationSerializer

    @extend_schema(
        description="Create customer from successful verification.",
        request=None,
        responses={201: structure_serializers.CustomerSerializer},
    )
    @action(detail=True, methods=["post"])
    def create_customer(self, request, uuid=None):
        """
        Create customer from successful verification.

        Returns the serialized customer details.
        """
        verification = self.get_object()

        try:
            customer = verification.create_customer_if_verified()
            serializer = structure_serializers.CustomerSerializer(
                customer, context=self.get_serializer_context()
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValueError as e:
            raise exceptions.ValidationError(str(e))
        except Exception as e:
            raise exceptions.APIException(f"Failed to create customer: {str(e)}")

    create_customer_serializer_class = structure_serializers.CustomerSerializer


class OnboardingJustificationViewSet(core_views.ActionsViewSet):
    """
    ViewSet for managing onboarding justifications.
    """

    queryset = OnboardingJustification.objects.all()
    serializer_class = OnboardingJustificationSerializer
    lookup_field = "uuid"
    filter_backends = (core_filters.StaffOrUserFilter, DjangoFilterBackend)

    @extend_schema(
        description="Create justification for failed verification.",
        request=OnboardingJustificationCreateSerializer,
        responses=OnboardingJustificationSerializer,
    )
    @action(detail=False, methods=["post"])
    def create_justification(self, request):
        """
        Create a new justification for a failed verification.
        """
        serializer = OnboardingJustificationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        verification = OnboardingVerification.objects.get(
            uuid=serializer.validated_data["verification_uuid"]
        )

        # Check if user owns the verification
        if verification.user != request.user:
            return Response(
                {"error": "You can only justify your own verifications"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Create justification
        justification = OnboardingJustification.objects.create(
            verification=verification,
            user=request.user,
            user_justification=serializer.validated_data["user_justification"],
        )

        response_serializer = OnboardingJustificationSerializer(justification)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        description="Attach supporting document to justification.",
        request=OnboardingJustificationDocumentationSerializer,
        responses=OnboardingJustificationDocumentationSerializer,
    )
    @action(detail=True, methods=["post"])
    def attach_document(self, request, uuid=None):
        """
        Attach supporting documentation to a justification.

        Users can upload multiple documents to support their justification
        for manual review when automatic validation fails.
        """
        justification = self.get_object()

        serializer = OnboardingJustificationDocumentationSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        documentation = serializer.save(justification=justification)

        response_serializer = OnboardingJustificationDocumentationSerializer(
            documentation
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    attach_document_serializer_class = OnboardingJustificationDocumentationSerializer

    @extend_schema(
        description="Approve justification and mark verification as VERIFIED.",
        request=OnboardingJustificationReviewSerializer,
        responses=OnboardingJustificationSerializer,
    )
    @action(
        detail=True, methods=["post"], permission_classes=[core_permissions.IsStaff]
    )
    def approve(self, request, uuid=None):
        """
        Approve a justification and update verification status to VERIFIED.
        """
        justification = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        staff_notes = serializer.validated_data.get("staff_notes", "")

        justification.approve_justification(request.user, staff_notes)

        response_serializer = OnboardingJustificationSerializer(justification)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    approve_serializer_class = OnboardingJustificationReviewSerializer

    @extend_schema(
        description="Reject justification and mark verification as FAILED.",
        request=OnboardingJustificationReviewSerializer,
        responses=OnboardingJustificationSerializer,
    )
    @action(
        detail=True, methods=["post"], permission_classes=[core_permissions.IsStaff]
    )
    def reject(self, request, uuid=None):
        """
        Reject a justification and update verification status to FAILED.
        """
        justification = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        staff_notes = serializer.validated_data.get("staff_notes", "")

        justification.reject_justification(request.user, staff_notes)

        response_serializer = OnboardingJustificationSerializer(justification)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    reject_serializer_class = OnboardingJustificationReviewSerializer


class SupportedCountriesView(APIView):
    """
    API view to get list of supported countries for validation.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        description="Return list of supported countries for validation.",
        responses={
            200: inline_serializer(
                name="SupportedCountriesResponse",
                fields={
                    "supported_countries": serializers.ListField(
                        child=serializers.CharField()
                    ),
                },
            )
        },
    )
    def get(self, request):
        """Return list of supported countries."""
        countries = onboarding_validator.get_supported_countries()
        return Response({"supported_countries": countries})


class OnboardingCountryChecklistConfigurationViewSet(core_views.ActionsViewSet):
    queryset = OnboardingCountryChecklistConfiguration.objects.all()
    serializer_class = OnboardingCountryChecklistConfigurationSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.OnboardingCountryChecklistConfigurationFilter
    lookup_field = "uuid"
    permission_classes = (core_permissions.IsStaff,)


class OnboardingQuestionMetadataViewSet(core_views.ActionsViewSet):
    queryset = OnboardingQuestionMetadata.objects.all()
    serializer_class = OnboardingQuestionMetadataSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.OnboardingQuestionMetadataFilter
    lookup_field = "uuid"
    permission_classes = (core_permissions.IsStaff,)
