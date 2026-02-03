from datetime import timedelta

from constance import config
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import exceptions, permissions, serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.checklist.mixins import UserChecklistMixin
from waldur_core.core import filters as core_filters
from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.structure import models as structure_models
from waldur_core.structure import serializers as structure_serializers

from . import enums, filters, tasks
from .backends import backend_registry
from .models import (
    OnboardingJustification,
    OnboardingQuestionMetadata,
    OnboardingVerification,
)
from .serializers import (
    OnboardingCompanyValidationRequestSerializer,
    OnboardingJustificationCreateSerializer,
    OnboardingJustificationDocumentationSerializer,
    OnboardingJustificationReviewSerializer,
    OnboardingJustificationSerializer,
    OnboardingQuestionMetadataSerializer,
    OnboardingRunValidationRequestSerializer,
    OnboardingVerificationSerializer,
    PersonIdentifierFieldsResponseSerializer,
)
from .validators import onboarding_validator


def check_legal_person_identifier_not_exists(justification):
    """
    Validator to check if requested customer already exists in Customer objects.

    Raises ValidationError if a customer with the same registration_code exists.
    """
    legal_person_identifier = justification.verification.legal_person_identifier

    if not legal_person_identifier:
        return

    existing_customer = structure_models.Customer.objects.filter(
        registration_code=legal_person_identifier
    ).first()

    if existing_customer:
        raise exceptions.ValidationError(
            f"A customer with registration code '{legal_person_identifier}' already exists. "
            f"Customer name: '{existing_customer.name}'. Cannot approve this justification."
        )


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
    filterset_class = filters.OnboardingVerificationFilter

    # Override later with correct permissions per action
    checklist_permissions = [permissions.IsAuthenticated]
    completion_status_permissions = [permissions.IsAuthenticated]
    submit_answers_permissions = [permissions.IsAuthenticated]

    def get_checklist_completion(self, obj, checklist_type=None):
        """
        Get checklist completion for the given verification.

        Args:
            obj: OnboardingVerification instance
            checklist_type: checklist_enums.ChecklistTypes value (ONBOARDING_CUSTOMER_DATA or ONBOARDING_INTENT_DATA).
                          If None, defaults to ONBOARDING_INTENT_DATA for backward compatibility.

        Returns:
            ChecklistCompletion instance or None
        """
        if checklist_type is None:
            checklist_type = checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
        return obj.get_or_create_checklist_completion(checklist_type)

    @extend_schema(
        description="Get checklist with questions and existing answers. "
        "Supports both customer and intent checklists via checklist_type parameter.",
        parameters=[
            inline_serializer(
                name="ChecklistParams",
                fields={
                    "checklist_type": serializers.ChoiceField(
                        choices=["customer", "intent"],
                        default="intent",
                        help_text="Type of checklist to retrieve (customer or intent). Defaults to intent.",
                    ),
                    "include_all": serializers.BooleanField(
                        default=False,
                        help_text="If true, returns all questions including hidden ones.",
                    ),
                },
            )
        ],
        responses=checklist_serializers.ChecklistResponseSerializer,
    )
    @action(detail=True, methods=["get"])
    def checklist(self, request, uuid=None):
        """Get checklist questions with existing answers for the current user."""
        verification = self.get_object()

        # Get checklist type from query params (default to INTENT)
        checklist_type_param = request.query_params.get(
            "checklist_type", "intent"
        ).lower()
        if checklist_type_param == "customer":
            checklist_type = checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
        elif checklist_type_param == "intent":
            checklist_type = checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
        else:
            return Response(
                {
                    "detail": f"Invalid checklist_type '{checklist_type_param}'. Must be 'customer' or 'intent'."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        completion = self.get_checklist_completion(verification, checklist_type)
        if not completion or not completion.checklist:
            return Response(
                {
                    "detail": f"No {checklist_type_param} checklist configured for this verification"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        checklist = completion.checklist

        # Check if client wants all questions (for dynamic form visibility)
        include_all = request.query_params.get("include_all", "false").lower() == "true"

        if include_all:
            questions = checklist.questions.all().order_by("order")
        else:
            questions = checklist.get_visible_questions(completion)

        # Create response data
        response_data = {
            "checklist": checklist,
            "completion": completion,
            "questions": questions,
        }

        response_serializer = checklist_serializers.ChecklistResponseSerializer(
            response_data, context={"request": request, "completion": completion}
        )
        return Response(response_serializer.data)

    @extend_schema(
        description="Get checklist completion status. "
        "Supports both customer and intent checklists via checklist_type parameter.",
        parameters=[
            inline_serializer(
                name="CompletionStatusParams",
                fields={
                    "checklist_type": serializers.ChoiceField(
                        choices=["customer", "intent"],
                        default="intent",
                        help_text="Type of checklist to retrieve (customer or intent). Defaults to intent.",
                    ),
                },
            )
        ],
        responses=checklist_serializers.ChecklistCompletionSerializer,
    )
    @action(detail=True, methods=["get"])
    def completion_status(self, request, uuid=None):
        """Get checklist completion status."""
        verification = self.get_object()

        # Get checklist type from query params (default to INTENT)
        checklist_type_param = request.query_params.get(
            "checklist_type", "intent"
        ).lower()
        if checklist_type_param == "customer":
            checklist_type = checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
        elif checklist_type_param == "intent":
            checklist_type = checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
        else:
            return Response(
                {
                    "detail": f"Invalid checklist_type '{checklist_type_param}'. Must be 'customer' or 'intent'."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        completion = self.get_checklist_completion(verification, checklist_type)
        if not completion or not completion.checklist:
            return Response(
                {
                    "detail": f"No {checklist_type_param} checklist configured for this verification"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        completion_data = checklist_serializers.ChecklistCompletionSerializer(
            completion, context={"request": request}
        ).data

        return Response(completion_data)

    @extend_schema(
        description="Submit answers to checklist questions. "
        "Automatically detects which checklist (customer or intent) each question belongs to.",
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses=OnboardingVerificationSerializer,
    )
    @action(detail=True, methods=["post"])
    def submit_answers(self, request, uuid=None):
        """Submit answers to either customer or intent checklist questions."""
        verification = self.get_object()

        # Get both completions
        customer_completion = verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
        )
        intent_completion = verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
        )

        # Group raw answer data by checklist type based on question's checklist
        answers_by_type = {
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA: [],
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA: [],
        }

        for answer_item in request.data:
            question_uuid = answer_item.get("question_uuid")
            if not question_uuid:
                return Response(
                    {"detail": "question_uuid is required for each answer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                question = checklist_models.Question.objects.get(uuid=question_uuid)
            except checklist_models.Question.DoesNotExist:
                return Response(
                    {"detail": f"Question {question_uuid} does not exist"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Check if question belongs to customer checklist
            if (
                customer_completion
                and customer_completion.checklist
                and customer_completion.checklist.questions.filter(
                    uuid=question.uuid
                ).exists()
            ):
                answers_by_type[
                    checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
                ].append(answer_item)
            # Check if question belongs to intent checklist
            elif (
                intent_completion
                and intent_completion.checklist
                and intent_completion.checklist.questions.filter(
                    uuid=question.uuid
                ).exists()
            ):
                answers_by_type[
                    checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
                ].append(answer_item)
            else:
                return Response(
                    {
                        "detail": f"Question {question_uuid} does not belong to any configured checklist for this verification"
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        for checklist_type, answer_data_list in answers_by_type.items():
            if not answer_data_list:
                continue

            completion = verification.get_or_create_checklist_completion(checklist_type)
            if not completion:
                continue

            serializer = checklist_serializers.AnswerSubmitSerializer(
                data=answer_data_list,
                many=True,
                context={"completion": completion, "request": request},
            )
            serializer.is_valid(raise_exception=True)

            for answer_data in serializer.validated_data:
                question = answer_data["question"]
                answer_value = answer_data["answer_data"]

                if answer_value is None:
                    # Remove answer (hard delete)
                    checklist_models.Answer.objects.filter(
                        completion=completion,
                        question=question,
                        user=request.user,
                    ).delete()
                else:
                    # Create or update answer
                    checklist_models.Answer.objects.update_or_create(
                        completion=completion,
                        question=question,
                        user=request.user,
                        defaults={"answer_data": answer_value},
                    )

            completion.update_completion_status()

        response_serializer = OnboardingVerificationSerializer(
            verification, context={"request": request}
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    def get_permissions(self):
        """Get permissions based on action."""
        # Map actions to their permission attributes
        permission_map = {
            "checklist": "checklist_permissions",
            "completion_status": "completion_status_permissions",
            "submit_answers": "submit_answers_permissions",
            "available_checklists": "available_checklists_permissions",
        }

        # If this is a checklist-related action, use its specific permissions
        if self.action in permission_map:
            permission_attr = permission_map[self.action]
            permission_classes = getattr(self, permission_attr, [])
            return [permission() for permission in permission_classes]

        # Otherwise, use default permissions from parent class
        return super().get_permissions()

    @extend_schema(
        description="Get available onboarding checklists (customer and intent) for preview. "
        "This endpoint allows users to see checklist questions before creating a verification. "
        "Supports checklist_type parameter to filter by customer or intent checklists. "
        "Includes questions with onboarding metadata (field mappings).",
        parameters=[
            inline_serializer(
                name="AvailableChecklistsParams",
                fields={
                    "checklist_type": serializers.ChoiceField(
                        choices=["customer", "intent", "all"],
                        default="all",
                        help_text="Type of checklist to retrieve (customer, intent, or all). Defaults to all.",
                    ),
                },
            )
        ],
        responses={
            200: inline_serializer(
                name="AvailableChecklistsResponse",
                fields={
                    "customer_checklist": serializers.DictField(allow_null=True),
                    "intent_checklist": serializers.DictField(allow_null=True),
                },
            )
        },
    )
    @action(detail=False, methods=["get"])
    def available_checklists(self, request):
        """
        Returns customer and/or intent checklists with their questions.
        This allows users to preview what information will be needed before starting verification.
        Includes onboarding-specific metadata for each question.
        """
        checklist_type_param = request.query_params.get("checklist_type", "all").lower()

        result = {}

        # Helper to fetch and serialize checklist with questions
        def get_checklist_data(checklist_type):
            try:
                checklist = checklist_models.Checklist.objects.get(
                    checklist_type=checklist_type
                )
                checklist_data = checklist_serializers.ChecklistSerializer(
                    checklist, context={"request": request}
                ).data

                # Get questions for this checklist
                questions = checklist.questions.all().order_by("order")
                questions_data = checklist_serializers.QuestionWithAnswerSerializer(
                    questions, many=True, context={"request": request}
                ).data

                # Add onboarding metadata to questions
                for question_data in questions_data:
                    question_uuid = question_data.get("uuid")
                    try:
                        metadata = OnboardingQuestionMetadata.objects.get(
                            question__uuid=question_uuid
                        )
                        question_data["onboarding_metadata"] = {
                            "maps_to_customer_field": metadata.maps_to_customer_field,
                            "intent_field": metadata.intent_field,
                        }
                    except OnboardingQuestionMetadata.DoesNotExist:
                        question_data["onboarding_metadata"] = None

                checklist_data["questions"] = questions_data
                return checklist_data
            except checklist_models.Checklist.DoesNotExist:
                return None

        # Get requested checklist(s)
        if checklist_type_param in ["customer", "all"]:
            result["customer_checklist"] = get_checklist_data(
                checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
            )

        if checklist_type_param in ["intent", "all"]:
            result["intent_checklist"] = get_checklist_data(
                checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
            )

        if checklist_type_param not in ["customer", "intent", "all"]:
            return Response(
                {
                    "detail": f"Invalid checklist_type '{checklist_type_param}'. Must be 'customer', 'intent', or 'all'."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(result)

    available_checklists_permissions = [permissions.IsAuthenticated]

    @extend_schema(
        description="Start company validation process by creating a verification record. "
        "User selects validation_method (e.g., 'ariregister', 'wirtschaftscompass'). "
        "Checklists are used for intent and customer data collection. "
        "Then call run_validation to perform automatic validation or create manual justification.",
        request=OnboardingCompanyValidationRequestSerializer,
        responses=OnboardingVerificationSerializer,
    )
    @action(detail=False, methods=["post"])
    def start_verification(self, request):
        """
        Start company validation process by creating a verification record.

        User selects validation_method to specify which automatic validation backend to use.
        Creates OnboardingVerification with required fields for automatic validation.
        Checklists (ONBOARDING_INTENT_DATA and optionally ONBOARDING_CUSTOMER_DATA)
        are used for supplemental data collection.
        """
        serializer = OnboardingCompanyValidationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        validation_method = serializer.validated_data.get("validation_method", "")

        # Create verification record
        verification = OnboardingVerification.objects.create(
            user=request.user,
            validation_method=validation_method,
            country=serializer.validated_data.get("country", ""),
            legal_person_identifier=serializer.validated_data.get(
                "legal_person_identifier", ""
            ),
            legal_name=serializer.validated_data.get("legal_name", ""),
        )

        if not validation_method:
            verification.status = enums.VerificationStatus.ESCALATED
            expire_delta = config.ONBOARDING_VERIFICATION_EXPIRY_HOURS
            verification.expires_at = timezone.now() + timedelta(hours=expire_delta)
            verification.save()

        # Always create INTENT checklist (required for all validation types)
        verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
        )
        # Also create CUSTOMER checklist (primarily for manual validation, skipped for automatic)
        verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
        )

        # Return the verification result
        response_serializer = OnboardingVerificationSerializer(
            verification, context={"request": request}
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    start_verification_serializer_class = OnboardingCompanyValidationRequestSerializer

    @extend_schema(
        description="Run automatic validation using the required fields provided during verification creation. "
        "Checklist answers (if any) are only used for supplemental customer/intent data.",
        request=OnboardingRunValidationRequestSerializer,
        responses=OnboardingVerificationSerializer,
    )
    @action(detail=True, methods=["post"])
    def run_validation(self, request, uuid=None):
        """
        Run automatic validation using verification data.

        Uses the validation_method and required fields (legal_person_identifier, legal_name)
        provided during verification creation.
        Checklist answers are only used for supplemental customer data, not for verification fields.
        Runs validation backend and updates verification status.
        """
        verification = self.get_object()

        # Ensure validation_method is set
        if not verification.validation_method:
            raise exceptions.ValidationError(
                "No validation_method specified. This verification requires manual approval."
            )

        # ToDo: remove this after implementing getting user's identifier via auth methods
        # Accept optional person_identifier and Austrian data from request body if provided
        serializer = OnboardingRunValidationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Convert birth_date to string format if provided
        birth_date_value = serializer.validated_data.get("birth_date")
        birth_date_str = (
            birth_date_value.strftime("%Y-%m-%d") if birth_date_value else ""
        )

        verification = onboarding_validator.validate_company(
            user=request.user,
            validation_method=verification.validation_method,
            legal_person_identifier=verification.legal_person_identifier,
            legal_name=verification.legal_name,
            existing_verification=verification,
            # ToDo: remove this after implementing getting user's identifier via auth methods
            person_identifier=serializer.validated_data.get("civil_number", ""),
            first_name=serializer.validated_data.get("first_name", ""),
            last_name=serializer.validated_data.get("last_name", ""),
            birth_date=birth_date_str,
        )

        response_serializer = OnboardingVerificationSerializer(
            verification, context={"request": request}
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    run_validation_serializer_class = OnboardingRunValidationRequestSerializer

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
    filterset_class = filters.OnboardingJustificationFilter

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
            user_justification=serializer.validated_data.get(
                "user_justification", None
            ),
        )

        if verification.status == enums.VerificationStatus.VERIFIED:
            verification.status = enums.VerificationStatus.ESCALATED
            verification.error_traceback = _(
                "Justification created for already automatically verified company. Verification set to ESCALATED for manual review."
            )
            verification.save()

        response_serializer = OnboardingJustificationSerializer(
            justification, context=self.get_serializer_context()
        )
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

        response_serializer = OnboardingJustificationSerializer(
            justification, context=self.get_serializer_context()
        )
        tasks.send_justification_review_notification.delay(justification.uuid)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    approve_serializer_class = OnboardingJustificationReviewSerializer
    approve_validators = [check_legal_person_identifier_not_exists]

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

        response_serializer = OnboardingJustificationSerializer(
            justification, context=self.get_serializer_context()
        )
        tasks.send_justification_review_notification.delay(justification.uuid)
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


class PersonIdentifierFieldsView(APIView):
    """
    API view to get person identifier field requirements for a specific validation method.

    This endpoint allows clients to query the exact person identifier fields needed
    for a particular validation method (e.g., 'ariregister', 'wirtschaftscompass').
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        description=(
            "Return person identifier field specification for a specific validation method. "
            "The validation_method parameter should match one of the available methods "
            "(e.g., 'ariregister', 'wirtschaftscompass', 'bolagsverket', 'breg')."
        ),
        parameters=[
            OpenApiParameter(
                name="validation_method",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=True,
                enum=[choice[0] for choice in enums.ValidationMethod.CHOICES],
                description="Validation method identifier",
            )
        ],
        responses=PersonIdentifierFieldsResponseSerializer,
    )
    def get(self, request):
        """Return person identifier fields for the specified validation method."""
        validation_method = request.query_params.get("validation_method")

        if not validation_method:
            return Response(
                {"error": "validation_method query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fields = backend_registry.get_person_identifier_fields_for_method(
            validation_method
        )

        if fields is None:
            return Response(
                {
                    "error": f"Fields are not configured for validation method '{validation_method}'"
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        response_serializer = PersonIdentifierFieldsResponseSerializer(
            {
                "validation_method": validation_method,
                "person_identifier_fields": fields,
            }
        )

        return Response(response_serializer.data)


class OnboardingQuestionMetadataViewSet(core_views.ActionsViewSet):
    queryset = OnboardingQuestionMetadata.objects.all()
    serializer_class = OnboardingQuestionMetadataSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.OnboardingQuestionMetadataFilter
    lookup_field = "uuid"
    permission_classes = [permissions.IsAuthenticated]
    create_permissions = update_permissions = partial_update_permissions = (
        delete_permissions
    ) = [core_permissions.IsStaff]
