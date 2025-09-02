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

from drf_spectacular.utils import extend_schema
from rest_framework import decorators, response, status
from rest_framework import permissions as rf_permissions

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers


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
    checklist_permissions = [rf_permissions.IsAdminUser]
    completion_status_permissions = [rf_permissions.IsAdminUser]
    submit_answers_permissions = [rf_permissions.IsAdminUser]

    @extend_schema(
        description="Get checklist with questions and existing answers.",
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

        # Get visible questions using checklist module logic
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
                # Create or update answer using direct foreign key
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
    checklist_review_permissions = [rf_permissions.IsAdminUser]
    completion_review_status_permissions = [rf_permissions.IsAdminUser]

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
