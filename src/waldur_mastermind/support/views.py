from waldur_core.core.serializers import StatusSerializer
from rest_framework import status
import logging
from datetime import date, datetime

from constance import config
from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes
from rest_framework import (
    decorators,
    generics,
    permissions,
    response,
    status,
    views,
    viewsets,
)
from rest_framework import exceptions as rf_exceptions
from rest_framework.exceptions import ValidationError

from waldur_core.core import mixins as core_mixins
from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.core.serializers import EmptySerializer
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure import (
    exceptions as structure_exceptions,
)
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import (
    permissions as structure_permissions,
)
from waldur_mastermind.notifications.models import BroadcastMessage
from waldur_mastermind.support.backend.atlassian_discovery import (
    AtlassianDiscoveryError,
    AtlassianDiscoveryService,
    TemporaryCredentials,
)
from waldur_mastermind.support.backend.smax import SmaxServiceBackend
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend

from . import backend, exceptions, executors, filters, models, serializers, tasks

logger = logging.getLogger(__name__)


class CheckExtensionMixin(core_views.ConstanceCheckExtensionMixin):
    extension_name = "WALDUR_SUPPORT"


class IssueViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Issue.objects.all()
    lookup_field = "uuid"
    filter_backends = (
        filters.IssueCallerOrRoleFilterBackend,
        DjangoFilterBackend,
        filters.IssueResourceFilterBackend,
    )
    filterset_class = filters.IssueFilter
    serializer_class = serializers.IssueSerializer

    def can_create_user(request, view, obj=None):
        if not request.user.email:
            raise rf_exceptions.ValidationError(
                _(
                    "Current user does not have email, "
                    "therefore he is not allowed to create issues."
                )
            )

        if not request.user.full_name:
            raise rf_exceptions.ValidationError(
                _(
                    "Current user does not have full_name, "
                    "therefore he is not allowed to create issues."
                )
            )

    @transaction.atomic()
    def perform_create(self, serializer):
        issue: models.Issue = serializer.save()
        try:
            backend.get_active_backend().create_issue(issue)
            backend.get_active_backend().create_confirmation_comment(issue)
        except exceptions.SupportUserInactive:
            raise rf_exceptions.ValidationError({"caller": _("Caller is inactive.")})
        except structure_exceptions.ServiceBackendError as e:
            raise rf_exceptions.ValidationError(e)

    create_permissions = [can_create_user]

    @transaction.atomic()
    def perform_update(self, serializer):
        issue: models.Issue = serializer.save()
        backend.get_active_backend().update_issue(issue)

    def _update_is_available_validator(issue):
        if not backend.get_active_backend().update_is_available(issue):
            raise ValidationError("Updating is not available.")

    update_permissions = partial_update_permissions = [
        structure_permissions.is_staff_or_support
    ]
    update_validators = partial_update_validators = [_update_is_available_validator]

    @transaction.atomic()
    def perform_destroy(self, issue):
        backend.get_active_backend().delete_issue(issue)
        issue.delete()

    def _destroy_is_available_validator(issue):
        if not backend.get_active_backend().destroy_is_available(issue):
            raise ValidationError("Destroying is not available.")

    destroy_permissions = [structure_permissions.is_staff_or_support]
    destroy_validators = [_destroy_is_available_validator]

    def _comment_permission(request, view, obj: models.Issue | None = None):
        user = request.user
        if user.is_staff or user.is_support or not obj:
            return
        issue = obj
        # if it's a personal issue
        if not issue.customer and not issue.project and issue.caller == user:
            return
        if issue.customer and issue.customer.has_user(user, CustomerRole.OWNER):
            return
        if issue.project and (
            issue.project.has_user(user, ProjectRole.ADMIN)
            or issue.project.has_user(user, ProjectRole.MANAGER)
        ):
            return
        raise rf_exceptions.PermissionDenied()

    def _comment_create_is_available_validator(issue):
        if not backend.get_active_backend().comment_create_is_available(issue):
            raise ValidationError("Creating is not available.")

    @extend_schema(responses={status.HTTP_201_CREATED: serializers.CommentSerializer})
    @decorators.action(detail=True, methods=["post"])
    def comment(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            comment = serializer.save()
            backend.get_active_backend().create_comment(comment)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    comment_serializer_class = serializers.CommentSerializer
    comment_permissions = [_comment_permission]
    comment_validators = [_comment_create_is_available_validator]

    @extend_schema(responses={status.HTTP_200_OK: None}, request=None)
    @decorators.action(detail=True, methods=["post"])
    def sync(self, request, uuid=None):
        issue: models.Issue = self.get_object()
        backend.get_active_backend().sync_issues(issue.id)
        return response.Response(status=status.HTTP_200_OK)

    sync_permissions = [structure_permissions.is_staff_or_support]


class PriorityViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Priority.objects.all().order_by("name")
    serializer_class = serializers.PrioritySerializer
    filterset_class = filters.PriorityFilter
    lookup_field = "uuid"


class RequestTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for listing active request types.

    Only returns request types that are marked as active.
    Order is determined by the 'order' field (lowest first).
    """

    queryset = models.RequestType.objects.filter(is_active=True)
    serializer_class = serializers.RequestTypeSerializer
    lookup_field = "uuid"


class RequestTypeAdminViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    """
    Admin endpoint for managing request types.

    Staff only. Returns all request types (active and inactive).
    Supports full CRUD operations plus activate/deactivate actions.
    """

    queryset = models.RequestType.objects.all()
    serializer_class = serializers.RequestTypeAdminSerializer
    lookup_field = "uuid"
    filterset_class = filters.RequestTypeFilter

    def get_queryset(self):
        return models.RequestType.objects.all().order_by("order", "name")

    list_permissions = retrieve_permissions = create_permissions = (
        update_permissions
    ) = partial_update_permissions = destroy_permissions = [
        structure_permissions.is_staff
    ]

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer}, request=None)
    @decorators.action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        """Activate a request type so it appears in issue creation."""
        instance = self.get_object()
        instance.is_active = True
        instance.save(update_fields=["is_active"])
        return response.Response({"status": "activated"})

    activate_permissions = [structure_permissions.is_staff]

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer}, request=None)
    @decorators.action(detail=True, methods=["post"])
    def deactivate(self, request, uuid=None):
        """Deactivate a request type so it no longer appears in issue creation."""
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=["is_active"])
        return response.Response({"status": "deactivated"})

    deactivate_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={status.HTTP_200_OK: StatusSerializer},
        request=serializers.RequestTypeReorderSerializer,
    )
    @decorators.action(detail=False, methods=["post"])
    def reorder(self, request):
        """Bulk update order for multiple request types."""
        serializer = serializers.RequestTypeReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data["items"]
        for item in items:
            models.RequestType.objects.filter(uuid=item["uuid"]).update(
                order=item["order"]
            )
        return response.Response({"status": "reordered"})

    reorder_permissions = [structure_permissions.is_staff]


class CommentViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.CommentSerializer
    filter_backends = (
        filters.CommentIssueCallerOrRoleFilterBackend,
        DjangoFilterBackend,
        filters.CommentIssueResourceFilterBackend,
    )
    filterset_class = filters.CommentFilter
    queryset = models.Comment.objects.select_related("author__user", "issue").all()
    disabled_actions = ["create"]

    @transaction.atomic()
    def perform_update(self, serializer):
        comment: models.Comment = serializer.save()
        backend.get_active_backend().update_comment(comment)

    def _update_is_available_validator(comment):
        if not backend.get_active_backend().comment_update_is_available(comment):
            raise ValidationError("Updating is not available.")

    update_permissions = partial_update_permissions = [structure_permissions.is_staff]
    update_validators = partial_update_validators = [_update_is_available_validator]

    @transaction.atomic()
    def perform_destroy(self, comment):
        backend.get_active_backend().delete_comment(comment)
        comment.delete()

    def _destroy_is_available_validator(comment):
        if not backend.get_active_backend().comment_destroy_is_available(comment):
            raise ValidationError("Comment cannot be destroyed.")

    destroy_permissions = [structure_permissions.is_staff]
    destroy_validators = [_destroy_is_available_validator]

    def get_queryset(self):
        queryset = super().get_queryset()

        if not self.request.user.is_staff:
            subquery = Q(is_public=True) | Q(author__user=self.request.user)
            queryset = queryset.filter(subquery)

        return queryset


class SupportUserViewSet(CheckExtensionMixin, viewsets.ReadOnlyModelViewSet):
    queryset = models.SupportUser.objects.all()
    lookup_field = "uuid"
    permission_classes = (
        permissions.IsAuthenticated,
        structure_permissions.IsStaffOrSupportUser,
    )
    serializer_class = serializers.SupportUserSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SupportUserFilter


class SupportStatsViewSet(CheckExtensionMixin, generics.GenericAPIView):
    serializer_class = serializers.SupportStatsSerializer
    pagination_class = None

    def get(self, request, format=None):
        today = date.today()
        current_month = today.month
        open_issues_count = (
            models.Issue.objects.exclude(
                status__in=[
                    models.IssueStatus.Types.RESOLVED,
                    models.IssueStatus.Types.CANCELED,
                    "Closed",
                ]
            )
            .filter(resolution_date__isnull=True)
            .count()
        )
        closed_this_month_count = models.Issue.objects.filter(
            status__in=[models.IssueStatus.Types.RESOLVED, "Closed"],
            resolution_date__month=current_month,
        ).count()

        recent_broadcasts = BroadcastMessage.objects.filter(
            state=BroadcastMessage.States.SENT, created__month=current_month
        )
        recent_broadcasts_count = recent_broadcasts.count()

        data = {
            "open_issues_count": open_issues_count,
            "closed_this_month_count": closed_this_month_count,
            "recent_broadcasts_count": recent_broadcasts_count,
        }

        return JsonResponse(data)


class WebHookReceiverView(CheckExtensionMixin, views.APIView):
    authentication_classes = ()
    permission_classes = ()
    serializer_class = serializers.WebHookReceiverSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # Try to update order output if the issue is linked to an order (fail-safe)
        try:
            issue_key = serializer.validated_data["issue"]["key"]
            issue = serializer.get_issue(issue_key)
            from waldur_mastermind.marketplace import (
                models as marketplace_models,
            )

            if isinstance(issue.resource, marketplace_models.Order):
                self._update_order_output_from_webhook(
                    issue.resource, issue, "JIRA", request.data
                )
        except Exception as e:
            logger.warning(f"Failed to update order output from JIRA webhook: {e}")

        return response.Response(status=status.HTTP_200_OK)

    def _update_order_output_from_webhook(self, order, issue, source, webhook_data):
        """Update order output with webhook event info (fail-safe)."""
        try:
            # Parse existing webhook count from plain text output
            webhook_count = 1  # Default to 1 for new webhook
            if order.output:
                # Look for existing webhook count in the output
                lines = order.output.split("\n")
                for line in lines:
                    if "Webhook Events:" in line:
                        try:
                            webhook_count = (
                                int(line.split("Webhook Events:")[1].strip()) + 1
                            )
                        except (IndexError, ValueError):
                            webhook_count = 1
                        break

            # Create plain text output
            output_lines = [
                f"Issue: {issue.key} ({source})",
                f"Status: {issue.status}",
                f"Last Update: {datetime.now().isoformat()}",
                f"Webhook Events: {webhook_count}",
            ]

            order.output = "\n".join(output_lines)
            order.save(update_fields=["output"])
        except Exception as e:
            logger.error(f"Failed to update order output from webhook: {e}")


class AttachmentViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Attachment.objects.all()
    filterset_class = filters.AttachmentFilter
    filter_backends = [DjangoFilterBackend]
    serializer_class = serializers.AttachmentSerializer
    lookup_field = "uuid"
    disabled_actions = ["update", "partial_update"]

    @transaction.atomic()
    def perform_destroy(self, attachment):
        backend.get_active_backend().delete_attachment(attachment)
        attachment.delete()

    def _destroy_is_available_validator(attachment):
        if not backend.get_active_backend().attachment_destroy_is_available(attachment):
            raise ValidationError("Destroying is not available.")

    destroy_validators = [_destroy_is_available_validator]

    @transaction.atomic()
    def perform_create(self, serializer):
        attachment: models.Attachment = serializer.save()
        backend.get_active_backend().create_attachment(attachment)

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter_for_user(self.request.user)


class TemplateViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    permission_classes = [core_permissions.IsAdminOrReadOnly]
    queryset = models.Template.objects.all().order_by("name")
    lookup_field = "uuid"
    serializer_class = serializers.TemplateSerializer

    @extend_schema(
        description="This view attaches documents to template.",
        request=serializers.CreateAttachmentsSerializer,
        responses={201: None, 400: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def create_attachments(self, request, uuid=None):
        template: models.Template = self.get_object()
        attachments = request.FILES.getlist("attachments")

        if not attachments:
            return response.Response(
                {"detail": "No attachments provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        for attachment in attachments:
            obj, created = models.TemplateAttachment.objects.get_or_create(
                template=template,
                name=attachment.name,
                defaults={"file": attachment},
            )
            if created:
                template.attachments.add(obj)
        return response.Response(status=status.HTTP_201_CREATED)

    @extend_schema(request=serializers.DeleteAttachmentsSerializer, responses=None)
    @decorators.action(detail=True, methods=["post"])
    def delete_attachments(self, request, uuid=None):
        template: models.Template = self.get_object()
        attachment_ids = request.data.get("attachment_ids", [])
        attachments = models.TemplateAttachment.objects.filter(
            uuid__in=attachment_ids, template=template
        )
        attachments.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)


class FeedbackViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.Feedback.objects.all().order_by("created")
    disabled_actions = ["update", "partial_update", "destroy"]
    permission_classes = (core_permissions.ActionsPermission,)
    create_permissions = ()
    create_serializer_class = serializers.CreateFeedbackSerializer
    serializer_class = serializers.FeedbackSerializer
    create_executor = executors.FeedbackExecutor
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.FeedbackFilter

    list_permissions = retrieve_permissions = [
        structure_permissions.is_staff_or_support
    ]


class FeedbackReportViewSet(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = []
    serializer_class = EmptySerializer
    pagination_class = None

    def get(self, request, format=None):
        result = {
            str(count["evaluation"]): count["id__count"]
            for count in models.Feedback.objects.values("evaluation").annotate(
                Count("id")
            )
        }
        return response.Response(result, status=status.HTTP_200_OK)


class FeedbackAverageReportViewSet(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = []
    serializer_class = EmptySerializer
    pagination_class = None

    def get(self, request, format=None):
        avg = models.Feedback.objects.aggregate(Avg("evaluation"))["evaluation__avg"]

        if avg:
            result = round(avg, 2)
        else:
            result = None
        return response.Response(result, status=status.HTTP_200_OK)


class ZammadWebHookReceiverView(CheckExtensionMixin, generics.GenericAPIView):
    authentication_classes = ()
    permission_classes = ()
    filter_backends = []
    serializer_class = EmptySerializer
    pagination_class = None

    def post(self, request):
        ticket_id = request.data.get("ticket", {}).get("id")

        if not ticket_id:
            raise ValidationError("Key ticket.id is required.")

        issue: models.Issue = get_object_or_404(models.Issue, backend_id=ticket_id)
        logger.info(
            f"Updating issue {issue.key} based on data from ticket with id {ticket_id}."
        )
        ZammadServiceBackend().update_waldur_issue_from_zammad(issue)
        ZammadServiceBackend().update_waldur_comments_from_zammad(issue)

        # Update order output if issue is linked to an order (fail-safe)
        try:
            from waldur_mastermind.marketplace import models as marketplace_models

            if isinstance(issue.resource, marketplace_models.Order):
                self._update_order_output_from_webhook(
                    issue.resource, issue, "Zammad", request.data
                )
        except Exception as e:
            logger.warning(f"Failed to update order output from Zammad webhook: {e}")

        return response.Response(status=status.HTTP_200_OK)

    def _update_order_output_from_webhook(self, order, issue, source, webhook_data):
        """Update order output with webhook event info (fail-safe)."""
        try:
            import json
            from datetime import datetime

            existing_output = {}
            if order.output:
                try:
                    existing_output = json.loads(order.output)
                except json.JSONDecodeError:
                    existing_output = {}

            # Update with current webhook info
            existing_output.update(
                {
                    "timestamp": datetime.now().isoformat(),
                    "issue_key": issue.key,
                    "issue_status": issue.status,
                    "backend": source,
                    "last_webhook_update": datetime.now().isoformat(),
                }
            )

            # Track webhook events (keep last 5)
            if "webhook_events" not in existing_output:
                existing_output["webhook_events"] = []

            existing_output["webhook_events"].append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "source": source,
                    "ticket_id": str(
                        webhook_data.get("ticket", {}).get("id", "unknown")
                    ),
                }
            )
            existing_output["webhook_events"] = existing_output["webhook_events"][-5:]

            # Format as human-readable text for consistency
            output_lines = [
                f"Issue: {existing_output.get('issue_key', 'unknown')} ({source})",
                f"Status: {existing_output.get('issue_status', 'unknown')}",
                f"Last Update: {existing_output.get('timestamp', 'unknown')}",
                f"Webhook Events: {len(existing_output.get('webhook_events', []))}",
            ]
            order.output = "\n".join(output_lines)
            order.save(update_fields=["output"])
        except Exception as e:
            logger.error(f"Failed to update order output from webhook: {e}")


class SmaxWebHookReceiverView(CheckExtensionMixin, generics.GenericAPIView):
    authentication_classes = ()
    permission_classes = ()
    filter_backends = ()
    serializer_class = serializers.SmaxWebHookReceiverSerializer

    def post(self, request):
        issue_id = request.data.get("id")

        if not issue_id:
            raise ValidationError("Key id is required.")

        issue: models.Issue = get_object_or_404(
            models.Issue,
            backend_id=issue_id,
            backend_name=SmaxServiceBackend.backend_name,
        )
        logger.info(
            f"Syncing issue {issue.key} based on data from ticket with id {issue_id}."
        )
        SmaxServiceBackend().sync_single_issue(issue)

        # Update order output if issue is linked to an order (fail-safe)
        try:
            from waldur_mastermind.marketplace import models as marketplace_models

            if isinstance(issue.resource, marketplace_models.Order):
                self._update_order_output_from_webhook(
                    issue.resource, issue, "SMAX", request.data
                )
        except Exception as e:
            logger.warning(f"Failed to update order output from SMAX webhook: {e}")

        return response.Response(status=status.HTTP_200_OK)

    def _update_order_output_from_webhook(self, order, issue, source, webhook_data):
        """Update order output with webhook event info (fail-safe)."""
        try:
            from datetime import datetime

            # Parse existing webhook count from plain text output
            webhook_count = 1  # Default to 1 for new webhook
            if order.output:
                # Look for existing webhook count in the output
                lines = order.output.split("\n")
                for line in lines:
                    if "Webhook Events:" in line:
                        try:
                            webhook_count = (
                                int(line.split("Webhook Events:")[1].strip()) + 1
                            )
                        except (IndexError, ValueError):
                            webhook_count = 1
                        break

            # Create plain text output
            output_lines = [
                f"Issue: {issue.key} ({source})",
                f"Status: {issue.status}",
                f"Last Update: {datetime.now().isoformat()}",
                f"Webhook Events: {webhook_count}",
            ]

            order.output = "\n".join(output_lines)
            order.save(update_fields=["output"])
        except Exception as e:
            logger.error(f"Failed to update order output from webhook: {e}")


@extend_schema(
    request=None,
    responses={202: None, 403: None},
    description="This view triggers synchronization of issues from backend.",
)
@decorators.api_view(["GET", "POST"])
def sync_issues(request):
    if not request.user.is_active or not (
        request.user.is_staff or request.user.is_support
    ):
        return response.Response(status=status.HTTP_403_FORBIDDEN)

    tasks.sync_issues.delay()
    return response.Response(status=status.HTTP_202_ACCEPTED)


class IssueStatusViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.IssueStatus.objects.all().order_by("name")
    serializer_class = serializers.IssueStatusSerializer
    create_serializer_class = serializers.IssueStatusCreateSerializer
    update_serializer_class = serializers.IssueStatusCreateSerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    lookup_field = "uuid"
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]
    create_permissions = [core_permissions.IsStaff]
    update_permissions = [core_permissions.IsStaff]
    destroy_permissions = [core_permissions.IsStaff]


class AtlassianSettingsDiscoveryViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    """
    ViewSet for Atlassian settings discovery and configuration.

    Allows staff users to:
    1. Validate Atlassian credentials without saving
    2. Discover available projects, request types, custom fields, priorities
    3. Preview and save selected configuration to constance
    """

    queryset = models.Issue.objects.none()  # No model, stateless operations
    serializer_class = EmptySerializer

    def is_staff(request, view, obj=None):
        if not request.user.is_staff:
            raise rf_exceptions.PermissionDenied()

    def _get_discovery_service(self, credentials_data: dict):
        """Create discovery service from validated credentials."""
        creds = TemporaryCredentials(
            api_url=credentials_data["api_url"],
            auth_method=credentials_data["auth_method"],
            email=credentials_data.get("email"),
            token=credentials_data.get("token"),
            personal_access_token=credentials_data.get("personal_access_token"),
            username=credentials_data.get("username"),
            password=credentials_data.get("password"),
            verify_ssl=credentials_data.get("verify_ssl", True),
        )
        return AtlassianDiscoveryService(creds)

    @extend_schema(
        request=serializers.AtlassianCredentialsSerializer,
        responses={200: None},
        description="Validate Atlassian credentials without saving them.",
    )
    @decorators.action(detail=False, methods=["post"])
    def validate_credentials(self, request):
        """Validate Atlassian credentials without saving."""
        serializer = serializers.AtlassianCredentialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        result = service.validate_credentials()

        return response.Response(result, status=status.HTTP_200_OK)

    validate_credentials_serializer_class = serializers.AtlassianCredentialsSerializer
    validate_credentials_permissions = [is_staff]

    @extend_schema(
        request=serializers.DiscoverProjectsRequestSerializer,
        responses={200: serializers.AtlassianProjectResponseSerializer(many=True)},
        description="Discover available Service Desk projects.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_projects(self, request):
        """Discover available Service Desk projects."""
        serializer = serializers.DiscoverProjectsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            projects = service.discover_projects()
        except AtlassianDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.AtlassianProjectResponseSerializer(
            projects, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_projects_serializer_class = serializers.DiscoverProjectsRequestSerializer
    discover_projects_permissions = [is_staff]

    @extend_schema(
        request=serializers.DiscoverRequestTypesRequestSerializer,
        responses={200: serializers.AtlassianRequestTypeResponseSerializer(many=True)},
        description="Discover request types for a selected project.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_request_types(self, request):
        """Discover request types for a selected project."""
        serializer = serializers.DiscoverRequestTypesRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            request_types = service.discover_request_types(
                serializer.validated_data["project_id"]
            )
        except AtlassianDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.AtlassianRequestTypeResponseSerializer(
            request_types, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_request_types_serializer_class = (
        serializers.DiscoverRequestTypesRequestSerializer
    )
    discover_request_types_permissions = [is_staff]

    @extend_schema(
        request=serializers.DiscoverCustomFieldsRequestSerializer,
        responses={200: serializers.AtlassianCustomFieldResponseSerializer(many=True)},
        description="Discover available custom fields.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_custom_fields(self, request):
        """Discover available custom fields."""
        serializer = serializers.DiscoverCustomFieldsRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            fields = service.discover_custom_fields(
                project_id=serializer.validated_data.get("project_id"),
                request_type_id=serializer.validated_data.get("request_type_id"),
            )
        except AtlassianDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.AtlassianCustomFieldResponseSerializer(
            fields, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_custom_fields_serializer_class = (
        serializers.DiscoverCustomFieldsRequestSerializer
    )
    discover_custom_fields_permissions = [is_staff]

    @extend_schema(
        request=serializers.DiscoverPrioritiesRequestSerializer,
        responses={200: serializers.AtlassianPriorityResponseSerializer(many=True)},
        description="Discover available priorities.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_priorities(self, request):
        """Discover available priorities."""
        serializer = serializers.DiscoverPrioritiesRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            priorities = service.discover_priorities()
        except AtlassianDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.AtlassianPriorityResponseSerializer(
            priorities, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_priorities_serializer_class = (
        serializers.DiscoverPrioritiesRequestSerializer
    )
    discover_priorities_permissions = [is_staff]

    @extend_schema(
        request=serializers.AtlassianSettingsPreviewSerializer,
        responses={200: None},
        description="Generate preview of settings to be saved.",
    )
    @decorators.action(detail=False, methods=["post"])
    def preview_settings(self, request):
        """Generate preview of settings to be saved."""
        serializer = serializers.AtlassianSettingsPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Validate credentials first
        service = self._get_discovery_service(serializer.validated_data)
        validation = service.validate_credentials()

        if not validation.get("valid"):
            return response.Response(
                {
                    "valid": False,
                    "error": validation.get("error", "Invalid credentials"),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Build preview of settings
        data = serializer.validated_data
        preview = {
            "ATLASSIAN_API_URL": data["api_url"],
            "ATLASSIAN_PROJECT_ID": data["project_id"],
            "ATLASSIAN_VERIFY_SSL": data.get("verify_ssl", True),
            "ATLASSIAN_USE_OLD_API": data.get("use_old_api", False),
            "ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED": data.get(
                "custom_field_mapping_enabled", True
            ),
        }

        # Auth-specific settings based on method
        if data["auth_method"] == "api_token":
            preview["ATLASSIAN_EMAIL"] = data["email"]
            preview["ATLASSIAN_TOKEN"] = "***HIDDEN***"
        elif data["auth_method"] == "personal_access_token":
            preview["ATLASSIAN_PERSONAL_ACCESS_TOKEN"] = "***HIDDEN***"
        else:
            preview["ATLASSIAN_USERNAME"] = data["username"]
            preview["ATLASSIAN_PASSWORD"] = "***HIDDEN***"

        # Request types are now stored in database
        if data.get("issue_types"):
            preview["ACTIVE_REQUEST_TYPES"] = ", ".join(data["issue_types"])
            preview["_note_request_types"] = (
                "Selected types will be activated in the RequestType database table"
            )
        if data.get("reporter_field"):
            preview["ATLASSIAN_REPORTER_FIELD"] = data["reporter_field"]
        if data.get("impact_field"):
            preview["ATLASSIAN_IMPACT_FIELD"] = data["impact_field"]
        if data.get("organisation_field"):
            preview["ATLASSIAN_ORGANISATION_FIELD"] = data["organisation_field"]
        if data.get("project_field"):
            preview["ATLASSIAN_PROJECT_FIELD"] = data["project_field"]
        if data.get("affected_resource_field"):
            preview["ATLASSIAN_AFFECTED_RESOURCE_FIELD"] = data[
                "affected_resource_field"
            ]
        if data.get("caller_field"):
            preview["ATLASSIAN_CALLER_FIELD"] = data["caller_field"]
        if data.get("template_field"):
            preview["ATLASSIAN_TEMPLATE_FIELD"] = data["template_field"]
        if data.get("sla_field"):
            preview["ATLASSIAN_SLA_FIELD"] = data["sla_field"]
        if data.get("resolution_sla_field"):
            preview["ATLASSIAN_RESOLUTION_SLA_FIELD"] = data["resolution_sla_field"]
        if data.get("satisfaction_field"):
            preview["ATLASSIAN_SATISFACTION_FIELD"] = data["satisfaction_field"]
        if data.get("request_feedback_field"):
            preview["ATLASSIAN_REQUEST_FEEDBACK_FIELD"] = data["request_feedback_field"]
        if data.get("waldur_backend_id_field"):
            preview["ATLASSIAN_WALDUR_BACKEND_ID_FIELD"] = data[
                "waldur_backend_id_field"
            ]

        return response.Response(
            {
                "preview": preview,
                "message": "Review settings and call save_settings with confirm_save=True",
            },
            status=status.HTTP_200_OK,
        )

    preview_settings_serializer_class = serializers.AtlassianSettingsPreviewSerializer
    preview_settings_permissions = [is_staff]

    @extend_schema(
        request=serializers.AtlassianSettingsSaveSerializer,
        responses={200: None},
        description="Save selected settings to constance.",
    )
    @decorators.action(detail=False, methods=["post"])
    def save_settings(self, request):
        """Save selected settings to constance."""
        serializer = serializers.AtlassianSettingsSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Validate credentials first
        service = self._get_discovery_service(serializer.validated_data)
        validation = service.validate_credentials()

        if not validation.get("valid"):
            return response.Response(
                {
                    "saved": False,
                    "error": validation.get("error", "Invalid credentials"),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data

        # Save settings to constance
        try:
            # URL and options
            setattr(config, "ATLASSIAN_API_URL", data["api_url"])
            setattr(config, "ATLASSIAN_VERIFY_SSL", data.get("verify_ssl", True))

            # Auth credentials based on method
            if data["auth_method"] == "api_token":
                setattr(config, "ATLASSIAN_EMAIL", data["email"])
                setattr(config, "ATLASSIAN_TOKEN", data["token"])
                # Clear other auth methods
                setattr(config, "ATLASSIAN_PERSONAL_ACCESS_TOKEN", "")
                setattr(config, "ATLASSIAN_USERNAME", "")
                setattr(config, "ATLASSIAN_PASSWORD", "")
            elif data["auth_method"] == "personal_access_token":
                setattr(
                    config,
                    "ATLASSIAN_PERSONAL_ACCESS_TOKEN",
                    data["personal_access_token"],
                )
                # Clear other auth methods
                setattr(config, "ATLASSIAN_EMAIL", "")
                setattr(config, "ATLASSIAN_TOKEN", "")
                setattr(config, "ATLASSIAN_USERNAME", "")
                setattr(config, "ATLASSIAN_PASSWORD", "")
            else:
                setattr(config, "ATLASSIAN_USERNAME", data["username"])
                setattr(config, "ATLASSIAN_PASSWORD", data["password"])
                # Clear other auth methods
                setattr(config, "ATLASSIAN_EMAIL", "")
                setattr(config, "ATLASSIAN_TOKEN", "")
                setattr(config, "ATLASSIAN_PERSONAL_ACCESS_TOKEN", "")

            # Project settings
            setattr(config, "ATLASSIAN_PROJECT_ID", data["project_id"])
            setattr(config, "ATLASSIAN_USE_OLD_API", data.get("use_old_api", False))
            setattr(
                config,
                "ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED",
                data.get("custom_field_mapping_enabled", True),
            )

            # Activate selected request types in database
            if data.get("issue_types"):
                # Deactivate all request types first
                models.RequestType.objects.update(is_active=False)
                # Activate selected ones and set order
                for idx, name in enumerate(data["issue_types"]):
                    models.RequestType.objects.filter(name=name).update(
                        is_active=True, order=idx
                    )

            # Optional field mappings
            if data.get("reporter_field"):
                setattr(config, "ATLASSIAN_REPORTER_FIELD", data["reporter_field"])
            if data.get("impact_field"):
                setattr(config, "ATLASSIAN_IMPACT_FIELD", data["impact_field"])
            if data.get("organisation_field"):
                setattr(
                    config, "ATLASSIAN_ORGANISATION_FIELD", data["organisation_field"]
                )
            if data.get("project_field"):
                setattr(config, "ATLASSIAN_PROJECT_FIELD", data["project_field"])
            if data.get("affected_resource_field"):
                setattr(
                    config,
                    "ATLASSIAN_AFFECTED_RESOURCE_FIELD",
                    data["affected_resource_field"],
                )
            if data.get("caller_field"):
                setattr(config, "ATLASSIAN_CALLER_FIELD", data["caller_field"])
            if data.get("template_field"):
                setattr(config, "ATLASSIAN_TEMPLATE_FIELD", data["template_field"])
            if data.get("sla_field"):
                setattr(config, "ATLASSIAN_SLA_FIELD", data["sla_field"])
            if data.get("resolution_sla_field"):
                setattr(
                    config,
                    "ATLASSIAN_RESOLUTION_SLA_FIELD",
                    data["resolution_sla_field"],
                )
            if data.get("satisfaction_field"):
                setattr(
                    config, "ATLASSIAN_SATISFACTION_FIELD", data["satisfaction_field"]
                )
            if data.get("request_feedback_field"):
                setattr(
                    config,
                    "ATLASSIAN_REQUEST_FEEDBACK_FIELD",
                    data["request_feedback_field"],
                )
            if data.get("waldur_backend_id_field"):
                setattr(
                    config,
                    "ATLASSIAN_WALDUR_BACKEND_ID_FIELD",
                    data["waldur_backend_id_field"],
                )
            if data.get("default_offering_issue_type"):
                setattr(
                    config,
                    "ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE",
                    data["default_offering_issue_type"],
                )

            # Clear API configuration cache
            cache.delete("API_CONFIGURATION")

            return response.Response(
                {
                    "saved": True,
                    "message": "Atlassian settings saved successfully",
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.exception("Failed to save Atlassian settings")
            return response.Response(
                {
                    "saved": False,
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    save_settings_serializer_class = serializers.AtlassianSettingsSaveSerializer
    save_settings_permissions = [is_staff]

    @extend_schema(
        responses={200: None},
        description="Get current Atlassian settings (masked secrets).",
    )
    @decorators.action(detail=False, methods=["get"])
    def current_settings(self, request):
        """Get current Atlassian settings (masked secrets)."""
        # Get active request types from database
        active_request_types = list(
            models.RequestType.objects.filter(is_active=True)
            .order_by("order", "name")
            .values_list("name", flat=True)
        )

        settings_data = {
            "ATLASSIAN_API_URL": config.ATLASSIAN_API_URL,
            "ATLASSIAN_PROJECT_ID": config.ATLASSIAN_PROJECT_ID,
            "ATLASSIAN_VERIFY_SSL": config.ATLASSIAN_VERIFY_SSL,
            "ATLASSIAN_USE_OLD_API": config.ATLASSIAN_USE_OLD_API,
            # Issue types are now stored in RequestType model
            "ATLASSIAN_ISSUE_TYPES": active_request_types,
            "ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED": config.ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED,
            "ATLASSIAN_REPORTER_FIELD": config.ATLASSIAN_REPORTER_FIELD,
            "ATLASSIAN_IMPACT_FIELD": config.ATLASSIAN_IMPACT_FIELD,
            "ATLASSIAN_ORGANISATION_FIELD": config.ATLASSIAN_ORGANISATION_FIELD,
            "ATLASSIAN_PROJECT_FIELD": config.ATLASSIAN_PROJECT_FIELD,
            "ATLASSIAN_AFFECTED_RESOURCE_FIELD": config.ATLASSIAN_AFFECTED_RESOURCE_FIELD,
            "ATLASSIAN_CALLER_FIELD": config.ATLASSIAN_CALLER_FIELD,
            "ATLASSIAN_TEMPLATE_FIELD": config.ATLASSIAN_TEMPLATE_FIELD,
            "ATLASSIAN_SLA_FIELD": config.ATLASSIAN_SLA_FIELD,
            "ATLASSIAN_RESOLUTION_SLA_FIELD": config.ATLASSIAN_RESOLUTION_SLA_FIELD,
            "ATLASSIAN_SATISFACTION_FIELD": config.ATLASSIAN_SATISFACTION_FIELD,
            "ATLASSIAN_REQUEST_FEEDBACK_FIELD": config.ATLASSIAN_REQUEST_FEEDBACK_FIELD,
            "ATLASSIAN_WALDUR_BACKEND_ID_FIELD": config.ATLASSIAN_WALDUR_BACKEND_ID_FIELD,
            "ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE": config.ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE,
            # Credentials info for pre-filling (secrets are not returned)
            "ATLASSIAN_EMAIL": config.ATLASSIAN_EMAIL,
            "ATLASSIAN_USERNAME": config.ATLASSIAN_USERNAME,
            # Determine which auth method is configured
            "auth_method": (
                "api_token"
                if config.ATLASSIAN_TOKEN
                else (
                    "personal_access_token"
                    if config.ATLASSIAN_PERSONAL_ACCESS_TOKEN
                    else ("basic" if config.ATLASSIAN_PASSWORD else None)
                )
            ),
            "auth_configured": bool(
                config.ATLASSIAN_TOKEN
                or config.ATLASSIAN_PERSONAL_ACCESS_TOKEN
                or config.ATLASSIAN_PASSWORD
            ),
        }

        return response.Response(settings_data, status=status.HTTP_200_OK)

    current_settings_permissions = [is_staff]
