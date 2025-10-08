from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from waldur_core.core import filters as core_filters
from waldur_core.core import views as core_views
from waldur_core.structure import serializers as structure_serializers

from .models import OnboardingJustification, OnboardingVerification
from .serializers import (
    OnboardingCompanyValidationRequestSerializer,
    OnboardingJustificationCreateSerializer,
    OnboardingJustificationDocumentationSerializer,
    OnboardingJustificationSerializer,
    OnboardingVerificationSerializer,
)
from .validators import onboarding_validator


class OnboardingVerificationViewSet(core_views.ActionsViewSet):
    """
    ViewSet for managing company onboarding verifications.
    """

    queryset = OnboardingVerification.objects.all()
    serializer_class = OnboardingVerificationSerializer
    lookup_field = "uuid"
    filter_backends = (core_filters.StaffOrUserFilter, DjangoFilterBackend)

    @extend_schema(
        description="Start company validation process.",
        request=OnboardingCompanyValidationRequestSerializer,
        responses=OnboardingVerificationSerializer,
    )
    @action(detail=False, methods=["post"])
    def validate_company(self, request):
        """
        Start company validation process.

        Creates a new OnboardingVerification and runs validation.
        """
        serializer = OnboardingCompanyValidationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Start validation
        verification = onboarding_validator.validate_company(
            user=request.user,
            country=serializer.validated_data["country"],
            legal_person_identifier=serializer.validated_data[
                "legal_person_identifier"
            ],
            customer_data=serializer.validated_data["user_submitted_customer_metadata"],
            legal_name=serializer.validated_data.get("legal_name", ""),
        )

        # Return the verification result
        response_serializer = OnboardingVerificationSerializer(verification)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

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

        if not verification.can_create_customer():
            raise exceptions.ValidationError(
                "Cannot create customer: verification not valid or customer already exists"
            )

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


class SupportedCountriesView(APIView):
    """
    API view to get list of supported countries for validation.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        description="Return list of supported countries for validation.",
        responses={"supported_countries": list[str]},
    )
    def get(self, request):
        """Return list of supported countries."""
        countries = onboarding_validator.get_supported_countries()
        return Response({"supported_countries": countries})
