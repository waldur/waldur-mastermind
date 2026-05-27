"""ViewSet mixins for checklist functionality.

Usage Examples:

# For apps that need both user and reviewer functionality (like proposals):
class ProposalViewSet(UserChecklistMixin, ReviewerChecklistMixin, ActionsViewSet):
    # User permissions (managers can fill checklists)
    checklist_permissions = [permission_factory(PermissionEnum.MANAGE_PROPOSAL)]
    submit_answers_permissions = [permission_factory(PermissionEnum.MANAGE_PROPOSAL)]

    # Reviewer permissions (reviewers can see review logic)
    checklist_review_permissions = [permission_factory(PermissionEnum.MANAGE_PROPOSAL_REVIEW, ["round.call"])]

# For apps that only need user functionality (like projects):
class ProjectViewSet(UserChecklistMixin, ActionsViewSet):
    checklist_permissions = [permission_factory(PermissionEnum.MANAGE_PROJECT)]
    submit_answers_permissions = [permission_factory(PermissionEnum.MANAGE_PROJECT)]

# For dedicated reviewer ViewSets:
class ReviewerDashboardViewSet(ReviewerChecklistMixin, ReadOnlyActionsViewSet):
    checklist_review_permissions = [permission_factory(PermissionEnum.REVIEW_PROPOSALS)]

# Both mixins inherit from BaseChecklistMixin which provides:
# - get_checklist_completion(obj) -> ChecklistCompletion or None
# - get_checklist_for_object(obj) -> Checklist or None
"""

from drf_spectacular.plumbing import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import decorators, response, status

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.core.permissions import PATScopeAwareIsAdminUser


class BaseChecklistMixin:
    """Base mixin providing common checklist functionality.

    Provides shared helper methods used by both UserChecklistMixin and ReviewerChecklistMixin.
    Should not be used directly - use UserChecklistMixin or ReviewerChecklistMixin instead.
    """

    def get_checklist_completion(self, obj):
        """Get checklist completion for the given object.

        Default implementation assumes obj.checklist_completion exists.
        Override this method if your model uses a different pattern.

        Returns:
            ChecklistCompletion instance or None
        """
        return getattr(obj, "checklist_completion", None)

    def get_checklist_for_object(self, obj):
        """Get checklist for the given object.

        Default implementation uses completion.checklist.
        Can be overridden if needed.
        """
        completion = self.get_checklist_completion(obj)
        return completion.checklist if completion else None


class UserChecklistMixin(BaseChecklistMixin):
    """Mixin for ViewSets that provide checklist functionality to end users.

    Provides actions for users filling in checklists or viewing their answers:
    - checklist: Get checklist questions with existing answers (hides review logic)
    - completion_status: Get completion status (hides review triggers)
    - submit_answers: Submit answers to checklist questions

    Security Design:
    This mixin hides all review logic information to prevent users from gaming
    the system by seeing which answers trigger reviews.

    Default permissions are IsAdminUser but should be overridden with app-specific permissions:
    - checklist_permissions = [permission_factory(...)]
    - completion_status_permissions = [permission_factory(...)]
    - submit_answers_permissions = [permission_factory(...)]
    """

    # Default permissions - should be overridden by inheriting viewsets
    checklist_permissions = [PATScopeAwareIsAdminUser]
    completion_status_permissions = [PATScopeAwareIsAdminUser]
    submit_answers_permissions = [PATScopeAwareIsAdminUser]

    @extend_schema(
        description="Get checklist with questions and existing answers.",
        parameters=[
            OpenApiParameter(
                name="include_all",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description="If true, returns all questions including hidden ones (for dynamic form visibility). Default: false.",
                required=False,
            ),
        ],
        responses={
            200: checklist_serializers.ChecklistResponseSerializer,
            400: {"description": "No checklist configured"},
            404: {"description": "Object not found"},
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def checklist(self, request, uuid=None):
        """Get checklist questions with existing answers for the current user."""
        obj = self.get_object()

        completion = self.get_checklist_completion(obj)
        if not completion:
            return response.Response(
                {"detail": "No checklist configured for this object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        checklist = completion.checklist

        # Check if client wants all questions (for dynamic form visibility)
        # Support both DRF Request (query_params) and Django WSGIRequest (GET)
        query_params = getattr(request, "query_params", request.GET)
        include_all = query_params.get("include_all", "false").lower() == "true"

        if include_all:
            # Return ALL questions - frontend handles visibility dynamically
            questions = checklist.questions.all().order_by("order")
        else:
            # Return only visible questions based on saved answers (default)
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
        return response.Response(response_serializer.data)

    @extend_schema(
        description="Get checklist completion status.",
        responses={
            200: checklist_serializers.ChecklistCompletionSerializer,
            400: {"description": "No checklist configured"},
            404: {"description": "Object not found"},
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def completion_status(self, request, uuid=None):
        """Get checklist completion status."""
        obj = self.get_object()

        completion = self.get_checklist_completion(obj)
        if not completion:
            return response.Response(
                {"detail": "No checklist configured for this object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        completion_data = checklist_serializers.ChecklistCompletionSerializer(
            completion, context={"request": request}
        ).data

        return response.Response(completion_data)

    @extend_schema(
        description="Submit checklist answers.",
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses={
            200: checklist_serializers.AnswerSubmitResponseSerializer,
            400: {"description": "Validation error or no checklist configured"},
            404: {"description": "Object not found"},
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def submit_answers(self, request, uuid=None):
        """Submit checklist answers."""
        obj = self.get_object()

        completion = self.get_checklist_completion(obj)
        if not completion:
            return response.Response(
                {"detail": "No checklist configured for this object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate input data
        serializer = checklist_serializers.AnswerSubmitSerializer(
            data=request.data,
            many=True,
            context={"completion": completion, "request": request},
        )
        serializer.is_valid(raise_exception=True)

        # Process each answer
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
                # Create or update answer using direct foreign key.
                # File processing (for file/multiple_files questions) is handled
                # inside Answer.save() via process_file_answer(), which is idempotent.
                checklist_models.Answer.objects.update_or_create(
                    completion=completion,
                    question=question,
                    user=request.user,
                    defaults={"answer_data": answer_value},
                )

        # Update completion status to reflect any changes from additions/removals
        completion.update_completion_status()

        # Return updated completion status
        completion.refresh_from_db()

        # Create response data
        response_data = {
            "detail": "Answers submitted successfully",
            "completion": completion,
        }

        response_serializer = checklist_serializers.AnswerSubmitResponseSerializer(
            response_data, context={"request": request}
        )

        return response.Response(response_serializer.data)

    def get_checklist_for_new_object(self, parent_obj):
        """Get checklist that will be used for new objects.

        Override this method to provide the checklist that will be applied
        to new objects created under the given parent.

        Args:
            parent_obj: The parent object (e.g., Customer for new Projects)

        Returns:
            Checklist instance or None
        """
        return None

    @extend_schema(
        description="Get checklist template for creating new objects.",
        parameters=[
            OpenApiParameter(
                name="parent_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of the parent object (e.g., customer UUID for new projects)",
            )
        ],
        responses={
            200: checklist_serializers.ChecklistTemplateSerializer,
            400: {"description": "No checklist configured"},
            404: {"description": "Parent object not found"},
        },
    )
    @decorators.action(detail=False, methods=["get"], url_path="checklist-template")
    def checklist_template(self, request):
        """Get checklist template with questions for creating new objects.

        This action provides the checklist questions and visibility rules that
        apply when creating new objects. The frontend can use this to display
        the correct questions dynamically based on user answers.

        Query Parameters:
            parent_uuid: UUID of the parent object (e.g., customer UUID for new projects)

        Returns:
            Checklist template with all questions and their visibility rules.
        """
        # Support both DRF Request (query_params) and Django WSGIRequest (GET)
        query_params = getattr(request, "query_params", request.GET)
        parent_uuid = query_params.get("parent_uuid")
        if not parent_uuid:
            return response.Response(
                {"detail": "parent_uuid query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get parent object - this needs to be implemented by inheriting viewset
        parent_obj = self.get_parent_object_for_checklist(parent_uuid)
        if not parent_obj:
            return response.Response(
                {"detail": "Parent object not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Get the checklist for new objects
        checklist = self.get_checklist_for_new_object(parent_obj)
        if not checklist:
            return response.Response(
                {"detail": "No checklist configured for new objects"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create a temporary completion for visibility calculation
        # This is not saved to database
        temp_completion = checklist_models.ChecklistCompletion(
            checklist=checklist,
            scope=None,  # No scope yet as object doesn't exist
        )

        # Get all questions (no filtering by visibility yet)
        questions = checklist.questions.all().order_by("order")

        # Create response data
        response_data = {
            "checklist": checklist,
            "questions": questions,
            "initial_visible_questions": checklist.get_visible_questions(
                temp_completion
            ),
        }

        response_serializer = checklist_serializers.ChecklistTemplateSerializer(
            response_data, context={"request": request}
        )
        return response.Response(response_serializer.data)

    def get_parent_object_for_checklist(self, parent_uuid):
        """Get parent object for checklist template lookup.

        Must be overridden by inheriting viewsets to provide the parent object
        lookup logic (e.g., Customer.objects.get(uuid=parent_uuid) for ProjectViewSet).

        Args:
            parent_uuid: UUID of the parent object

        Returns:
            Parent object instance or None
        """
        raise NotImplementedError(
            "Subclasses must implement get_parent_object_for_checklist()"
        )


class ReviewerChecklistMixin(BaseChecklistMixin):
    """Mixin for ViewSets that provide checklist review functionality to reviewers.

    Provides actions for designated reviewers to view full checklist information
    including sensitive review logic:
    - checklist_review: Get full checklist with review logic exposed
    - completion_review_status: Get full completion status with review triggers exposed

    Security Design:
    This mixin exposes privileged review information and should only be used with
    proper reviewer permission controls.

    IMPORTANT: Must override permissions with app-specific reviewer checks:
    - checklist_review_permissions = [permission_factory(...)]  # Reviewer permissions required
    - completion_review_status_permissions = [permission_factory(...)]  # Reviewer permissions required
    """

    # Default permissions - MUST be overridden by inheriting viewsets with reviewer permissions
    checklist_review_permissions = [PATScopeAwareIsAdminUser]
    completion_review_status_permissions = [PATScopeAwareIsAdminUser]

    @extend_schema(
        description="Get checklist with questions and existing answers including review logic (reviewers only).",
        responses={
            200: checklist_serializers.ChecklistReviewerResponseSerializer,
            400: {"description": "No checklist configured"},
            404: {"description": "Object not found"},
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def checklist_review(self, request, uuid=None):
        """Get checklist questions with existing answers and review logic for reviewers."""
        obj = self.get_object()

        completion = self.get_checklist_completion(obj)
        if not completion:
            return response.Response(
                {"detail": "No checklist configured for this object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        checklist = completion.checklist

        # Get visible questions using checklist module logic
        questions = checklist.get_visible_questions(completion)

        # Create response data
        response_data = {
            "checklist": checklist,
            "completion": completion,
            "questions": questions,
        }

        response_serializer = checklist_serializers.ChecklistReviewerResponseSerializer(
            response_data, context={"request": request, "completion": completion}
        )
        return response.Response(response_serializer.data)

    @extend_schema(
        description="Get checklist completion status with review triggers (reviewers only).",
        responses={
            200: checklist_serializers.ChecklistCompletionReviewerSerializer,
            400: {"description": "No checklist configured"},
            404: {"description": "Object not found"},
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def completion_review_status(self, request, uuid=None):
        """Get checklist completion status with review information for reviewers."""
        obj = self.get_object()

        completion = self.get_checklist_completion(obj)
        if not completion:
            return response.Response(
                {"detail": "No checklist configured for this object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        completion_data = checklist_serializers.ChecklistCompletionReviewerSerializer(
            completion, context={"request": request}
        ).data

        return response.Response(completion_data)


# TODO: Add mixins for aggregated views (i.e. customerviewset, protectedcallview, etc)
