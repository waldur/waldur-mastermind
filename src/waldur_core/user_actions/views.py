import logging

from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, filters, permissions, status, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from waldur_core.core.permissions import PATScopeAwareIsAdminUser
from waldur_core.core.serializers import StatusSerializer

from . import filters as user_action_filters
from . import models, serializers, tasks

logger = logging.getLogger(__name__)


class UserActionViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for user actions"""

    queryset = models.UserAction.objects.select_related("content_type").all()
    serializer_class = serializers.UserActionSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = user_action_filters.UserActionFilter
    ordering = ["-urgency", "due_date", "-created"]
    ordering_fields = ["urgency", "due_date", "created", "modified", "action_type"]
    lookup_field = "uuid"

    def get_queryset(self):
        queryset = super().get_queryset()

        user_uuid = self.request.query_params.get("user_uuid")
        if user_uuid and self.request.user.is_staff:
            # Staff can view actions for a specific user
            queryset = queryset.filter(user__uuid=user_uuid)
        else:
            # Non-staff users can only see their own actions
            queryset = queryset.filter(user=self.request.user)

        # Only show non-silenced by default unless explicitly requested
        if self.request.query_params.get("include_silenced") != "true":
            now = timezone.now()
            queryset = queryset.filter(is_silenced=False).exclude(
                silenced_until__gt=now
            )

        return queryset

    def get_object(self):
        """Override to ensure users can only access their own actions"""
        obj = super().get_object()

        user_uuid = self.request.query_params.get("user_uuid")
        if user_uuid and self.request.user.is_staff:
            return obj

        if obj.user != self.request.user:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You can only access your own actions.")

        return obj

    @extend_schema(
        request=serializers.SilenceActionSerializer,
        responses={200: serializers.SilenceActionResponseSerializer},
        description="Silence an action temporarily or permanently",
    )
    @decorators.action(detail=True, methods=["post"])
    def silence(self, request, uuid=None):
        """Silence an action temporarily or permanently"""
        action = self.get_object()
        serializer = serializers.SilenceActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        duration_days = serializer.validated_data.get("duration_days")
        action.silence(duration_days=duration_days)

        logger.info(
            f"User {request.user.username} silenced action {action.uuid} "
            f"for {duration_days or 'permanent'} days"
        )

        response_data = {"status": "silenced", "duration_days": duration_days}
        response_serializer = serializers.SilenceActionResponseSerializer(response_data)
        return Response(response_serializer.data)

    @extend_schema(
        request=None,
        responses={200: StatusSerializer},
        description="Remove silence from an action",
    )
    @decorators.action(detail=True, methods=["post"])
    def unsilence(self, request, uuid=None):
        """Remove silence from an action"""
        # Override get_object for unsilence to include silenced actions
        try:
            # Get the original queryset without silencing filters
            full_queryset = models.UserAction.objects.select_related(
                "content_type"
            ).filter(user=self.request.user)
            action = full_queryset.get(uuid=uuid)
        except models.UserAction.DoesNotExist:
            raise NotFound("Action not found")

        action.unsilence()

        logger.info(f"User {request.user.username} unsilenced action {action.uuid}")

        response_data = {"status": "unsilenced"}
        response_serializer = StatusSerializer(response_data)
        return Response(response_serializer.data)

    @extend_schema(
        responses={200: serializers.UserActionSummarySerializer},
        description="Get action summary counts",
    )
    @decorators.action(detail=False, methods=["get"])
    def summary(self, request):
        """Get action summary counts"""
        queryset = self.filter_queryset(self.get_queryset())

        now = timezone.now()

        # Calculate overdue actions
        overdue_count = queryset.filter(
            due_date__lt=now, due_date__isnull=False
        ).count()

        # Count by urgency
        urgency_counts = {}
        for choice in models.UserAction.UrgencyChoices:
            urgency_counts[choice.value] = queryset.filter(urgency=choice.value).count()

        # Count by action type
        type_counts = {}
        action_types = queryset.values_list("action_type", flat=True).distinct()
        for action_type in action_types:
            type_counts[action_type] = queryset.filter(action_type=action_type).count()

        summary_data = {
            "total": queryset.count(),
            "overdue": overdue_count,
            "by_urgency": urgency_counts,
            "by_type": type_counts,
        }

        serializer = serializers.UserActionSummarySerializer(summary_data)
        return Response(serializer.data)

    @extend_schema(
        request=serializers.ExecuteActionSerializer,
        responses={
            200: serializers.ExecuteActionResponseSerializer,
            404: serializers.ExecuteActionErrorResponseSerializer,
            500: serializers.ExecuteActionErrorResponseSerializer,
        },
        description="Execute a corrective action",
    )
    @decorators.action(detail=True, methods=["post"])
    def execute_action(self, request, uuid=None):
        """Execute a corrective action"""
        action = self.get_object()
        serializer = serializers.ExecuteActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        action_label = serializer.validated_data["action_label"]

        # Find the corrective action
        corrective_actions = action.get_corrective_actions_for_user(request.user)
        corrective_action = next(
            (ca for ca in corrective_actions if ca.label == action_label), None
        )

        if not corrective_action:
            error_data = {"error": "Action not found or not permitted"}
            error_serializer = serializers.ExecuteActionErrorResponseSerializer(
                error_data
            )
            return Response(error_serializer.data, status=status.HTTP_404_NOT_FOUND)

        # Execute the action
        try:
            result = self._execute_corrective_action(request, action, corrective_action)

            # Log successful execution
            models.UserActionExecution.objects.create(
                action=action,
                user=request.user,
                corrective_action_label=action_label,
                success=True,
                execution_metadata=result.get("metadata", {}),
            )

            logger.info(
                f"User {request.user.username} executed action '{action_label}' "
                f"on {action.uuid}"
            )

            # Trigger immediate cleanup of stale actions for this user
            # This ensures the user sees updated action list immediately
            tasks.cleanup_stale_actions.delay(request.user.id, action.action_type)

            response_serializer = serializers.ExecuteActionResponseSerializer(result)
            return Response(response_serializer.data)

        except Exception as e:
            # Log failed execution
            models.UserActionExecution.objects.create(
                action=action,
                user=request.user,
                corrective_action_label=action_label,
                success=False,
                error_message=str(e),
            )

            logger.error(
                f"Failed to execute action '{action_label}' for user "
                f"{request.user.username} on {action.uuid}: {e}"
            )

            error_data = {"error": str(e)}
            error_serializer = serializers.ExecuteActionErrorResponseSerializer(
                error_data
            )
            return Response(
                error_serializer.data, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _execute_corrective_action(self, request, action, corrective_action):
        """Execute a corrective action"""
        if not corrective_action.api_endpoint:
            # Return route info for frontend navigation
            return {
                "action": "navigate",
                "route_name": corrective_action.route_name,
                "route_params": corrective_action.route_params,
            }

        # Handle API endpoint execution
        if corrective_action.method in ["POST", "PUT", "PATCH"]:
            return self._execute_api_action(request, action, corrective_action)

        return {
            "action": "navigate",
            "route_name": corrective_action.route_name,
            "route_params": corrective_action.route_params,
        }

    def _execute_api_action(self, request, action, corrective_action):
        """Execute an API-based corrective action"""
        from .providers import get_provider

        # Check if provider handles the action
        provider = get_provider(action.action_type)
        if provider:
            result = provider.execute_action(
                request.user,
                corrective_action,
                action.related_object,
                request=request,
                user_action=action,
            )
            if result:
                return result

        if (
            action.action_type == "pending_order"
            and "approve" in corrective_action.label.lower()
        ):
            # Example: Order approval
            order = action.related_object
            # This would call your existing order approval logic
            # order.approve(user)

            # For now, just return a success message
            return {
                "action": "completed",
                "message": "Order approval initiated",
                "route_name": corrective_action.route_name,
                "route_params": corrective_action.route_params,
                "metadata": {"order_uuid": str(order.uuid)},
            }

        # Add more action type handlers as needed
        raise NotImplementedError(
            f"API action not implemented: {corrective_action.label}"
        )

    @extend_schema(
        request=serializers.SilenceActionSerializer,
        responses={200: serializers.BulkSilenceResponseSerializer},
        description="Bulk silence actions by filters",
    )
    @decorators.action(detail=False, methods=["post"])
    def bulk_silence(self, request):
        """Bulk silence actions by filters"""
        # Get the queryset based on current filters
        queryset = self.filter_queryset(self.get_queryset())

        serializer = serializers.SilenceActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        duration_days = serializer.validated_data.get("duration_days")

        # Apply silence to all matching actions
        count = 0
        for action in queryset:
            action.silence(duration_days=duration_days)
            count += 1

        logger.info(
            f"User {request.user.username} bulk silenced {count} actions "
            f"for {duration_days or 'permanent'} days"
        )

        response_data = {
            "status": "bulk_silenced",
            "count": count,
            "duration_days": duration_days,
        }
        response_serializer = serializers.BulkSilenceResponseSerializer(response_data)
        return Response(response_serializer.data)

    # Serializer class specifications for action methods
    silence_serializer_class = serializers.SilenceActionSerializer
    execute_action_serializer_class = serializers.ExecuteActionSerializer
    bulk_silence_serializer_class = serializers.SilenceActionSerializer
    update_actions_serializer_class = serializers.UpdateActionsSerializer

    @extend_schema(
        request=serializers.UpdateActionsSerializer,
        responses={202: serializers.UpdateActionsResponseSerializer},
        description="Trigger update of user actions",
    )
    @decorators.action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAuthenticated],
    )
    def update_actions(self, request):
        """Trigger update of user actions"""
        provider_action_type = request.data.get("provider_action_type")

        # Trigger the celery task
        tasks.update_user_actions.delay(provider_action_type=provider_action_type)

        logger.info(
            f"User {request.user.username} triggered user actions update "
            f"for provider: {provider_action_type or 'all'}"
        )

        response_data = {
            "status": "scheduled",
            "message": "User actions update has been scheduled",
            "provider_action_type": provider_action_type,
        }
        response_serializer = serializers.UpdateActionsResponseSerializer(response_data)
        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)


class UserActionExecutionViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing action execution history"""

    serializer_class = serializers.UserActionExecutionSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering = ["-executed_at"]

    def get_queryset(self):
        # Prevent errors during schema generation
        if getattr(self, "swagger_fake_view", False):
            return models.UserActionExecution.objects.none()

        # SECURITY: Filter by current user - users can only see their own execution history
        return models.UserActionExecution.objects.filter(
            user=self.request.user
        ).select_related("action")


class UserActionProviderViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing registered action providers (admin only)"""

    queryset = models.UserActionProvider.objects.all()
    serializer_class = serializers.UserActionProviderSerializer
    permission_classes = [permissions.IsAuthenticated, PATScopeAwareIsAdminUser]
    filter_backends = [DjangoFilterBackend]

    def get_queryset(self):
        # Only allow staff to view providers - double check in case permission_classes fails
        if not self.request.user.is_staff:
            return models.UserActionProvider.objects.none()
        return super().get_queryset()
