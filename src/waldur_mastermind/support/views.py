import hmac
import logging
from datetime import date, datetime

from constance import config
from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, Count, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
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
from waldur_core.core.serializers import EmptySerializer, StatusSerializer
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure import (
    exceptions as structure_exceptions,
)
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import (
    permissions as structure_permissions,
)
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.notifications.models import BroadcastMessage

# The Atlassian discovery service imports atlassian-python-api, which eagerly pulls
# its whole API surface (~43 modules) at import. Atlassian is one of several optional
# support backends, so its symbols are imported lazily inside the discovery ViewSet
# methods below to keep it out of startup memory in deployments running a different
# (or no) support backend. See the "Lazy imports for heavy optional backends" section
# of CLAUDE.md.
from waldur_mastermind.support.backend.smax import SmaxServiceBackend
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend

from . import backend, exceptions, executors, filters, models, serializers, tasks

logger = logging.getLogger(__name__)


class CheckExtensionMixin(core_views.ConstanceCheckExtensionMixin):
    extension_name = "WALDUR_SUPPORT"


def get_provider_helpdesk_ids(user) -> set:
    """Helpdesks the user speaks for: as service-provider owner, or as agent.

    Empty for a user with no provider relationship, which is what callers use
    to decide whether a non-staff user may see provider-scoped data at all.
    """
    owned = models.ProviderHelpdesk.objects.filter(
        service_provider__customer__in=get_connected_customers(user, CustomerRole.OWNER)
    ).values_list("id", flat=True)
    agent_of = models.ProviderSupportUser.objects.filter(
        user=user, is_active=True
    ).values_list("provider_helpdesk_id", flat=True)
    return set(owned) | set(agent_of)


def validate_status_change_allowed(issue):
    """Only Waldur's own, unrouted issues may have their status written here."""
    if not backend.get_active_backend().update_is_available(issue):
        raise ValidationError("Updating is not available.")
    # A routed issue belongs to the provider's helpdesk: its status arrives over
    # that provider's webhook, and writing it here would be silently overwritten
    # by the next inbound sync.
    if issue.provider_helpdesk_id:
        raise ValidationError(
            "Issue is routed to a provider helpdesk, which owns its status."
        )


class IssueViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Issue.objects.prefetch_related(
        Prefetch(
            "child_issues",
            queryset=models.Issue.objects.select_related(
                "provider_helpdesk__service_provider__customer"
            ),
        )
    )
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

    def _set_status(self, issue, new_status):
        """Apply a status change through the backend that owns the issue.

        Raises ValidationError rather than letting SupportBackendError escape:
        it does not derive from ServiceBackendError, so an illegal transition
        would otherwise surface as a 500.
        """
        issue.status = new_status
        try:
            backend.get_active_backend().update_issue(issue)
        except backend.SupportBackendError as e:
            raise ValidationError(str(e))

    @extend_schema(
        summary="Move an issue to another status",
        request=serializers.SetIssueStatusSerializer,
        responses={200: serializers.IssueSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def set_status(self, request, uuid=None):
        """Change the status of an issue Waldur itself owns.

        `_update_is_available_validator` keeps this off the externally-backed
        issues: Jira, Zammad and SMAX inherit `update_is_available` as False, so
        their status stays whatever the remote service desk last told us.
        """
        issue = self.get_object()
        serializer = self.get_serializer(
            data=request.data, context={**self.get_serializer_context(), "issue": issue}
        )
        serializer.is_valid(raise_exception=True)

        self._set_status(issue, serializer.validated_data["status"])

        return response.Response(
            serializers.IssueSerializer(
                issue, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    set_status_permissions = [structure_permissions.is_staff_or_support]
    set_status_validators = [validate_status_change_allowed]
    set_status_serializer_class = serializers.SetIssueStatusSerializer

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
        # Provider support users can comment on tickets routed to their helpdesk.
        if (
            issue.provider_helpdesk
            and issue.provider_helpdesk.support_users.filter(
                user=user, is_active=True
            ).exists()
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

    @extend_schema(
        summary="Escalate an issue",
        request=serializers.EscalateIssueSerializer,
        responses={200: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def escalate(self, request, uuid=None):
        issue = self.get_object()
        ser = serializers.EscalateIssueSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data["reason"]

        from django.utils import timezone as tz

        issue.is_escalated = True
        issue.escalated_at = tz.now()
        issue.escalation_reason = reason
        issue.save(update_fields=["is_escalated", "escalated_at", "escalation_reason"])

        # Create escalation comment on the issue
        from . import models as support_models

        author_user = request.user
        support_user, _ = support_models.SupportUser.objects.get_or_create_from_user(
            author_user
        )
        support_models.Comment.objects.create(
            issue=issue,
            author=support_user,
            description=f"[ESCALATED] {reason}",
            is_public=True,
        )

        # Notify
        issue_id = issue.id
        transaction.on_commit(
            lambda: tasks.notify_ticket_escalated.delay(issue_id, reason)
        )
        transaction.on_commit(
            lambda: tasks.notify_provider_escalation.delay(issue_id, reason)
        )

        return response.Response(
            {"status": "escalated", "reason": reason},
            status=status.HTTP_200_OK,
        )

    def _escalate_permission(request, view, obj=None):
        user = request.user
        if user.is_staff or user.is_support:
            return
        if obj and obj.caller == user:
            return
        raise rf_exceptions.PermissionDenied()

    escalate_permissions = [_escalate_permission]
    escalate_serializer_class = serializers.EscalateIssueSerializer

    @extend_schema(
        summary="Bulk update multiple issues",
        request=serializers.BulkUpdateIssueSerializer,
        responses={200: None},
    )
    @decorators.action(detail=False, methods=["post"])
    def bulk_update(self, request):
        ser = serializers.BulkUpdateIssueSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        issues = models.Issue.objects.filter(uuid__in=data["issue_uuids"])
        found_count = issues.count()
        if found_count == 0:
            raise ValidationError("No issues found with the given UUIDs.")

        if "status" in data:
            # A raw queryset.update() here used to let a status be written for
            # any backend, including the externally-backed ones whose status
            # belongs to the remote service desk. It also skipped the field
            # tracker, so no transition check, no resolution date, and none of
            # the handlers that complete a support-offering order ever ran.
            #
            # Every issue is checked before any is written. Rejecting halfway
            # through would leave the batch half-applied: DRF turns the
            # ValidationError into a 400 response inside the atomic block, and
            # its set_rollback() is a no-op unless the database is configured
            # with ATOMIC_REQUESTS, which Waldur does not use. The operator
            # would see a failure with some tickets already moved.
            targets = list(issues)
            active_backend = backend.get_active_backend()
            for issue in targets:
                validate_status_change_allowed(issue)
                if data["status"] not in active_backend.get_available_statuses(issue):
                    raise ValidationError(
                        f"Issue {issue.key or issue.uuid.hex} cannot be moved from "
                        f"'{issue.status}' to '{data['status']}'."
                    )
            for issue in targets:
                self._set_status(issue, data["status"])
        if "priority" in data:
            issues.update(priority=data["priority"])
        if "assignee" in data:
            issues.update(assignee=data["assignee"])

        return response.Response(
            {"updated_count": found_count},
            status=status.HTTP_200_OK,
        )

    bulk_update_permissions = [structure_permissions.is_staff_or_support]
    bulk_update_serializer_class = serializers.BulkUpdateIssueSerializer

    @extend_schema(
        summary="Attach a marketplace resource to an issue",
        request=serializers.AttachResourceSerializer,
        responses={200: serializers.IssueSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def attach_resource(self, request, uuid=None):
        issue = self.get_object()

        if issue.resource_object_id:
            return response.Response(
                {"detail": "Issue already has a resource attached."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = serializers.AttachResourceSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        ser.is_valid(raise_exception=True)
        resource = ser.validated_data["resource"]

        issue.resource = resource
        issue.save(update_fields=["resource_content_type", "resource_object_id"])

        return response.Response(
            serializers.IssueSerializer(
                issue, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    attach_resource_permissions = [structure_permissions.is_staff_or_support]
    attach_resource_serializer_class = serializers.AttachResourceSerializer

    @extend_schema(
        summary="Manually route an issue to a provider helpdesk",
        request=serializers.RouteToProviderSerializer,
        responses={200: serializers.IssueSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def route_to_provider(self, request, uuid=None):
        issue = self.get_object()

        if issue.child_issues.exists():
            return response.Response(
                {"detail": "Issue is already routed to a provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = serializers.RouteToProviderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        provider_helpdesk = ser.validated_data["provider_helpdesk"]

        try:
            with transaction.atomic():
                child_issue = tasks.create_provider_child_issue(
                    issue, provider_helpdesk, issue.resource
                )
        except Exception:
            logger.exception(
                "Failed to manually route issue %s to provider %s.",
                issue.key,
                provider_helpdesk,
            )
            return response.Response(
                {"detail": "Failed to route issue to the selected provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        child_issue_id = child_issue.id
        transaction.on_commit(
            lambda: tasks.notify_provider_new_ticket.delay(child_issue_id)
        )

        return response.Response(
            serializers.IssueSerializer(
                issue, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    route_to_provider_permissions = [structure_permissions.is_staff_or_support]
    route_to_provider_serializer_class = serializers.RouteToProviderSerializer

    @extend_schema(
        summary="Re-route an already-routed issue to a different provider helpdesk",
        request=serializers.RouteToProviderSerializer,
        responses={200: serializers.IssueSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def reroute(self, request, uuid=None):
        issue = self.get_object()

        if not issue.child_issues.exists():
            return response.Response(
                {"detail": "Issue is not routed to a provider yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = serializers.RouteToProviderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_helpdesk = ser.validated_data["provider_helpdesk"]

        if issue.child_issues.filter(provider_helpdesk=new_helpdesk).exists():
            return response.Response(
                {"detail": "Issue is already routed to this provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                new_child, old_helpdesks = tasks.reroute_issue_to_provider(
                    issue, new_helpdesk
                )
        except Exception:
            logger.exception(
                "Failed to reroute issue %s to provider %s.", issue.key, new_helpdesk
            )
            return response.Response(
                {"detail": "Failed to reroute issue to the selected provider."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        issue_id = issue.id
        new_child_id = new_child.id
        old_helpdesk_ids = [helpdesk.id for helpdesk in old_helpdesks]
        transaction.on_commit(
            lambda: tasks.notify_provider_new_ticket.delay(new_child_id)
        )
        for helpdesk_id in old_helpdesk_ids:
            transaction.on_commit(
                lambda helpdesk_id=helpdesk_id: tasks.notify_provider_ticket_withdrawn.delay(
                    issue_id, helpdesk_id
                )
            )

        return response.Response(
            serializers.IssueSerializer(
                issue, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    reroute_permissions = [structure_permissions.is_staff_or_support]
    reroute_serializer_class = serializers.RouteToProviderSerializer


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


class SupportUserViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.SupportUser.objects.all()
    lookup_field = "uuid"
    serializer_class = serializers.SupportUserSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SupportUserFilter
    # Reads stay available to staff and support; all writes are staff-only.
    safe_methods_permissions = [structure_permissions.is_staff_or_support]
    unsafe_methods_permissions = [structure_permissions.is_staff]

    merge_serializer_class = serializers.SupportUserMergeSerializer
    merge_permissions = [structure_permissions.is_staff]

    connections_serializer_class = serializers.SupportUserConnectionsSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.SupportUserConnectionsSerializer},
    )
    @decorators.action(detail=True, methods=["get"])
    def connections(self, request, uuid=None):
        support_user = self.get_object()
        data = {
            "reported_issues": support_user.reported_issues.all(),
            "assigned_issues": support_user.issues.all(),
            "comments": support_user.comments.select_related("issue"),
            "attachments": support_user.attachments.select_related("issue"),
        }
        serializer = self.get_serializer(data)
        return response.Response(serializer.data)

    @extend_schema(
        request=serializers.SupportUserMergeSerializer,
        responses={status.HTTP_200_OK: serializers.SupportUserSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def merge(self, request, uuid=None):
        keeper = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sources = serializer.validated_data["source_users"]
        with transaction.atomic():
            for source in sources:
                source_id = source.uuid.hex
                # Issue.reporter / Issue.assignee are PROTECT: re-point before delete.
                reported = source.reported_issues.update(reporter=keeper)
                assigned = source.issues.update(assignee=keeper)
                # Comment.author / Attachment.author are CASCADE: re-point to
                # avoid silently deleting the merged user's comments and attachments.
                comments = source.comments.update(author=keeper)
                attachments = source.attachments.update(author=keeper)
                source.delete()
                # Audit trail for this destructive staff action; django-structlog
                # attaches the acting user and request id to the record.
                logger.info(
                    "Support user %s (backend_id=%s, backend_name=%s) merged into "
                    "%s by staff user %s. Re-pointed %d reported issue(s), "
                    "%d assigned issue(s), %d comment(s), %d attachment(s).",
                    source_id,
                    source.backend_id,
                    source.backend_name,
                    keeper.uuid.hex,
                    request.user.username,
                    reported,
                    assigned,
                    comments,
                    attachments,
                )
        return response.Response(
            serializers.SupportUserSerializer(
                keeper, context=self.get_serializer_context()
            ).data
        )


class ProviderTicketViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    """Provider's view of tickets routed to their helpdesk."""

    queryset = models.Issue.objects.filter(parent_issue__isnull=False)
    serializer_class = serializers.ProviderTicketSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProviderTicketFilter
    disabled_actions = ["create", "destroy"]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset

        return self.queryset.filter(
            provider_helpdesk_id__in=get_provider_helpdesk_ids(user)
        )

    @extend_schema(responses={status.HTTP_201_CREATED: None})
    @decorators.action(detail=True, methods=["post"])
    def comment(self, request, uuid=None):
        issue = self.get_object()
        ser = serializers.ProviderCommentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        support_user, _ = models.SupportUser.objects.get_or_create_from_user(
            request.user
        )
        with transaction.atomic():
            comment = models.Comment.objects.create(
                issue=issue,
                author=support_user,
                description=ser.validated_data["description"],
                is_public=ser.validated_data.get("is_public", True),
            )
            backend.get_active_backend().create_comment(comment)

        return response.Response(
            {"uuid": comment.uuid.hex, "description": comment.description},
            status=status.HTTP_201_CREATED,
        )

    comment_serializer_class = serializers.ProviderCommentSerializer

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer}, request=None)
    @decorators.action(detail=True, methods=["post"])
    def resolve(self, request, uuid=None):
        issue = self.get_object()
        issue.set_resolved()

        # Surface the resolution on the parent (operator) ticket WITHOUT
        # auto-closing it: log it and post a public comment so the operator
        # and caller are notified and can decide when to close the parent.
        # is_forwarded=True keeps the note from looping back to the child.
        if issue.parent_issue:
            parent = issue.parent_issue
            parent.append_processing_log(
                "child_resolved",
                {"child_key": issue.key},
            )
            parent.save(update_fields=["processing_log"])

            support_user, _created = models.SupportUser.objects.get_or_create_from_user(
                request.user
            )
            provider_name = (
                str(issue.provider_helpdesk.service_provider)
                if issue.provider_helpdesk
                else ""
            )
            models.Comment.objects.create(
                issue=parent,
                author=support_user,
                description=gettext(
                    "Provider %(provider)s resolved the routed ticket %(key)s."
                )
                % {"provider": provider_name, "key": issue.key},
                is_public=True,
                is_forwarded=True,
            )

        return response.Response({"status": "resolved"})

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def assign(self, request, uuid=None):
        issue = self.get_object()
        ser = serializers.ProviderAssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        provider_user = ser.validated_data["provider_support_user"]

        if provider_user.provider_helpdesk != issue.provider_helpdesk:
            raise ValidationError(
                "Support user does not belong to this issue's provider helpdesk."
            )

        issue.provider_assignee = provider_user
        issue.save(update_fields=["provider_assignee"])
        return response.Response({"status": "assigned"})

    assign_serializer_class = serializers.ProviderAssignSerializer

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def claim(self, request, uuid=None):
        issue = self.get_object()
        provider_user = models.ProviderSupportUser.objects.filter(
            user=request.user,
            provider_helpdesk=issue.provider_helpdesk,
            is_active=True,
        ).first()
        if not provider_user:
            raise ValidationError(
                "You are not a support user in this provider's helpdesk."
            )
        issue.provider_assignee = provider_user
        issue.save(update_fields=["provider_assignee"])
        return response.Response({"status": "claimed"})

    @extend_schema(
        summary="Get customer context for this ticket",
        responses={200: serializers.CustomerContextSerializer},
    )
    @decorators.action(detail=True, methods=["get"])
    def customer_context(self, request, uuid=None):
        issue = self.get_object()
        parent = issue.parent_issue

        caller_data = {
            "full_name": "",
            "email": "",
            "organization": "",
        }
        if parent and parent.caller:
            caller_data = {
                "full_name": parent.caller.full_name or "",
                "email": parent.caller.email or "",
                "organization": parent.customer.name if parent.customer else "",
            }

        resource_data = None
        if parent and parent.resource:
            resource_data = {
                "name": getattr(parent.resource, "name", str(parent.resource)),
                "type": getattr(parent.resource, "type", ""),
            }

        # Recent tickets from same caller
        recent_tickets = []
        if parent and parent.caller:
            recent = (
                models.Issue.objects.filter(caller=parent.caller)
                .exclude(pk=parent.pk)
                .order_by("-created")[:5]
            )
            recent_tickets = [
                {
                    "uuid": i.uuid,
                    "key": i.key,
                    "summary": i.summary,
                    "status": i.status,
                    "created": i.created,
                }
                for i in recent
            ]

        data = {
            "caller": caller_data,
            "resource": resource_data,
            "recent_tickets": recent_tickets,
        }
        return response.Response(data)

    @extend_schema(
        summary="Get statistics for provider tickets",
        responses={200: serializers.ProviderStatsSerializer},
    )
    @decorators.action(detail=False, methods=["get"])
    def stats(self, request):
        from django.db.models import Avg, ExpressionWrapper, F, fields

        qs = self.get_queryset()
        # Same open/closed definition as the support statistics and the is_open
        # filter. resolved_qs below still keys off resolution_date, because it
        # needs the timestamp to measure a duration.
        open_qs = qs.open()

        resolved_qs = qs.filter(resolution_date__isnull=False).annotate(
            resolve_time=ExpressionWrapper(
                F("resolution_date") - F("created"),
                output_field=fields.DurationField(),
            )
        )
        avg_resolve = resolved_qs.aggregate(avg=Avg("resolve_time"))["avg"]

        by_status = dict(
            open_qs.values_list("status")
            .annotate(count=Count("id"))
            .values_list("status", "count")
        )

        data = {
            "total_open": open_qs.count(),
            "total_resolved": qs.filter(resolution_date__isnull=False).count(),
            "total_escalated": qs.filter(is_escalated=True).count(),
            "sla_breach_count": qs.filter(sla_breached=True).count(),
            "avg_resolution_hours": (
                avg_resolve.total_seconds() / 3600 if avg_resolve else None
            ),
            "by_status": by_status,
        }
        return response.Response(data)


class ProviderHelpdeskViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.ProviderHelpdesk.objects.all()
    serializer_class = serializers.ProviderHelpdeskSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProviderHelpdeskFilter

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset
        provider_customers = get_connected_customers(user, CustomerRole.OWNER)
        return self.queryset.filter(service_provider__customer__in=provider_customers)

    def _is_owner_or_staff(request, view, obj=None):
        if request.user.is_staff:
            return
        # DRF invokes permissions at view-level with obj=None before the object
        # is loaded; defer to the object-level check instead of rejecting owners.
        if obj is None:
            return
        if obj.service_provider.customer.has_user(request.user, CustomerRole.OWNER):
            return
        raise rf_exceptions.PermissionDenied()

    # Creation has no object-level stage, so it stays staff-only; the owner
    # object-level check only makes sense for detail actions below.
    create_permissions = [structure_permissions.is_staff]
    update_permissions = partial_update_permissions = destroy_permissions = [
        _is_owner_or_staff
    ]

    @extend_schema(
        summary="Validate provider helpdesk backend connectivity",
        request=None,
        responses={200: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def validate(self, request, uuid=None):
        from django.utils import timezone

        from .backend import get_backend_for_provider

        helpdesk = self.get_object()
        try:
            get_backend_for_provider(helpdesk)
            helpdesk.last_health_check = timezone.now()
            helpdesk.last_health_status = "healthy"
            helpdesk.save(update_fields=["last_health_check", "last_health_status"])
            return response.Response(
                {"status": "healthy", "backend_type": helpdesk.backend_type}
            )
        except Exception as e:
            helpdesk.last_health_check = timezone.now()
            helpdesk.last_health_status = "unhealthy"
            helpdesk.save(update_fields=["last_health_check", "last_health_status"])
            return response.Response(
                {"status": "unhealthy", "error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    validate_permissions = [_is_owner_or_staff]


class ProviderSupportUserViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.ProviderSupportUser.objects.all()
    serializer_class = serializers.ProviderSupportUserSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProviderSupportUserFilter

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset
        provider_customers = get_connected_customers(user, CustomerRole.OWNER)
        return self.queryset.filter(
            provider_helpdesk__service_provider__customer__in=provider_customers
        )

    def _is_owner_or_staff(request, view, obj=None):
        if request.user.is_staff:
            return
        # DRF invokes permissions at view-level with obj=None before the object
        # is loaded; defer to the object-level check instead of rejecting owners.
        if obj is None:
            return
        if obj.provider_helpdesk.service_provider.customer.has_user(
            request.user, CustomerRole.OWNER
        ):
            return
        raise rf_exceptions.PermissionDenied()

    # Creation has no object-level stage, so it stays staff-only; the owner
    # object-level check only makes sense for detail actions below.
    create_permissions = [structure_permissions.is_staff]
    update_permissions = partial_update_permissions = destroy_permissions = [
        _is_owner_or_staff
    ]

    @extend_schema(
        summary="Get workload for all team members",
        responses={200: serializers.TeamWorkloadSerializer(many=True)},
    )
    @decorators.action(detail=False, methods=["get"])
    def team_workload(self, request):
        queryset = self.get_queryset().filter(is_active=True)
        data = [
            {
                "uuid": su.uuid,
                "user_full_name": su.user.full_name,
                "open_ticket_count": su.open_ticket_count,
                "max_open_tickets": su.max_open_tickets,
                "has_capacity": su.has_capacity,
            }
            for su in queryset.select_related("user")
        ]
        return response.Response(data)


class ProviderCannedResponseViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.ProviderCannedResponse.objects.all()
    serializer_class = serializers.ProviderCannedResponseSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProviderCannedResponseFilter

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset
        provider_customers = get_connected_customers(user, CustomerRole.OWNER)
        return self.queryset.filter(
            provider_helpdesk__service_provider__customer__in=provider_customers
        )

    def _is_owner_or_staff(request, view, obj=None):
        if request.user.is_staff:
            return
        # DRF invokes permissions at view-level with obj=None before the object
        # is loaded; defer to the object-level check instead of rejecting owners.
        if obj is None:
            return
        if obj.provider_helpdesk.service_provider.customer.has_user(
            request.user, CustomerRole.OWNER
        ):
            return
        raise rf_exceptions.PermissionDenied()

    # Creation has no object-level stage, so it stays staff-only; the owner
    # object-level check only makes sense for detail actions below.
    create_permissions = [structure_permissions.is_staff]
    update_permissions = partial_update_permissions = destroy_permissions = [
        _is_owner_or_staff
    ]

    @extend_schema(
        summary="Render a canned response with context variables",
        request=serializers.CannedResponseRenderSerializer,
        responses={200: serializers.CannedResponseRenderResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def render(self, request, uuid=None):
        canned_response = self.get_object()
        context_data = request.data.get("context", {})
        rendered = canned_response.render(context_data)
        canned_response.usage_count += 1
        canned_response.save(update_fields=["usage_count"])
        return response.Response({"rendered_text": rendered})

    render_serializer_class = serializers.CannedResponseRenderSerializer


class IssueTagViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.IssueTag.objects.all()
    serializer_class = serializers.IssueTagSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.IssueTagFilter

    list_permissions = retrieve_permissions = [
        structure_permissions.is_staff_or_support
    ]
    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff_or_support]


class IssueLinkViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.IssueLink.objects.all()
    serializer_class = serializers.IssueLinkSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.IssueLinkFilter

    list_permissions = retrieve_permissions = [
        structure_permissions.is_staff_or_support
    ]
    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff_or_support]


class SavedFilterViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.SavedFilter.objects.all()
    serializer_class = serializers.SavedFilterSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SavedFilterFilter

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset.filter(Q(user=user) | Q(is_shared=True))
        return self.queryset.filter(user=user)

    list_permissions = retrieve_permissions = create_permissions = [
        structure_permissions.is_staff_or_support
    ]

    def _is_owner_or_staff(request, view, obj=None):
        if request.user.is_staff:
            return
        if obj and obj.user == request.user:
            return
        raise rf_exceptions.PermissionDenied()

    update_permissions = partial_update_permissions = destroy_permissions = [
        _is_owner_or_staff
    ]


class CannedResponseViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.CannedResponse.objects.all()
    serializer_class = serializers.CannedResponseSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CannedResponseFilter

    list_permissions = retrieve_permissions = [
        structure_permissions.is_staff_or_support
    ]
    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff_or_support]

    @extend_schema(
        summary="Render a canned response with context variables",
        request=serializers.CannedResponseRenderSerializer,
        responses={200: serializers.CannedResponseRenderResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def render(self, request, uuid=None):
        canned_response = self.get_object()
        context_data = request.data.get("context", {})
        rendered = canned_response.render(context_data)
        return response.Response({"rendered_text": rendered})

    render_permissions = [structure_permissions.is_staff_or_support]
    render_serializer_class = serializers.CannedResponseRenderSerializer


class ProviderWebhookView(views.APIView):
    """Webhook endpoint for provider helpdesk backends.

    No authentication — validates via X-Webhook-Secret header
    matched against provider_helpdesk.webhook_secret.
    URL pattern: /api/support-provider-webhook/<uuid>/<backend_type>/
    """

    authentication_classes = ()
    permission_classes = ()
    serializer_class = serializers.WebhookPayloadSerializer

    def post(self, request, provider_uuid, backend_type):
        helpdesk = get_object_or_404(
            models.ProviderHelpdesk,
            uuid=provider_uuid,
            backend_type=backend_type,
            is_active=True,
        )

        secret = request.headers.get("X-Webhook-Secret", "")
        if not helpdesk.webhook_secret or secret != helpdesk.webhook_secret:
            return response.Response(
                {"error": "Invalid webhook secret"},
                status=status.HTTP_403_FORBIDDEN,
            )

        payload = request.data
        event_type = payload.get("event_type", "")

        if event_type == "comment_added":
            self._handle_comment(helpdesk, payload)
        elif event_type == "status_changed":
            self._handle_status_change(helpdesk, payload)
        else:
            logger.info(
                "Provider webhook received unknown event_type=%s for helpdesk=%s",
                event_type,
                helpdesk.uuid.hex,
            )

        helpdesk.last_health_check = date.today()
        helpdesk.last_health_status = "ok"
        helpdesk.save(update_fields=["last_health_check", "last_health_status"])

        return response.Response({"status": "ok"}, status=status.HTTP_200_OK)

    def _handle_comment(self, helpdesk, payload):
        issue_backend_id = payload.get("issue_backend_id")
        if not issue_backend_id:
            return
        try:
            child_issue = models.Issue.objects.get(
                backend_id=issue_backend_id,
                provider_helpdesk=helpdesk,
            )
        except models.Issue.DoesNotExist:
            logger.warning(
                "Webhook comment for unknown issue backend_id=%s", issue_backend_id
            )
            return

        comment_text = payload.get("comment", "")
        if comment_text and child_issue.parent_issue:
            support_user = None
            if helpdesk.service_provider.customer:
                su = models.SupportUser.objects.filter(
                    user__customerrole__customer=helpdesk.service_provider.customer
                ).first()
                if su:
                    support_user = su

            models.Comment.objects.create(
                issue=child_issue,
                author=support_user,
                description=comment_text,
                is_public=True,
                is_forwarded=False,
            )

    def _handle_status_change(self, helpdesk, payload):
        issue_backend_id = payload.get("issue_backend_id")
        new_status = payload.get("new_status")
        if not issue_backend_id or not new_status:
            return
        try:
            child_issue = models.Issue.objects.get(
                backend_id=issue_backend_id,
                provider_helpdesk=helpdesk,
            )
        except models.Issue.DoesNotExist:
            return

        # The provider owns this ticket's status, so the incoming value is
        # written as-is — but the resolution date still has to follow it, or the
        # SLA badge and the statistics never notice the ticket closing, and a
        # ticket the provider reopens stays closed here for good.
        child_issue.status = new_status
        updated_fields = ["status"]
        if child_issue.sync_resolution_date():
            updated_fields.append("resolution_date")
        child_issue.save(update_fields=updated_fields)


class HelpdeskStatsViewSet(CheckExtensionMixin, generics.GenericAPIView):
    """Comprehensive helpdesk statistics for staff/support users."""

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = serializers.HelpdeskStatsSerializer
    pagination_class = None

    @extend_schema(responses={200: serializers.HelpdeskStatsSerializer})
    def get(self, request, format=None):
        from .utils import get_helpdesk_stats

        stats = get_helpdesk_stats()
        return response.Response(stats)


class HelpdeskHealthViewSet(CheckExtensionMixin, generics.GenericAPIView):
    """Per-provider connectivity status."""

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = serializers.HelpdeskHealthSerializer
    queryset = models.ProviderHelpdesk.objects.none()
    pagination_class = None

    @extend_schema(responses={200: serializers.HelpdeskHealthSerializer(many=True)})
    def get(self, request, format=None):
        helpdesks = models.ProviderHelpdesk.objects.select_related(
            "service_provider__customer"
        ).all()
        data = [
            {
                "provider_name": str(h.service_provider),
                "backend_type": h.backend_type,
                "is_active": h.is_active,
                "health_status": h.health_status,
                "last_health_check": h.last_health_check,
                "failed_routing_count": h.failed_routing_count,
            }
            for h in helpdesks
        ]
        return response.Response(data)


class SupportStatsViewSet(CheckExtensionMixin, generics.GenericAPIView):
    """Ticket counts for the support dashboard.

    Staff and support see the whole deployment. A provider sees only the
    tickets routed to their own helpdesks, so these numbers never disclose one
    provider's volume to another. Everyone else is refused: these are
    operator-level figures, and until now any authenticated user could read
    them.
    """

    serializer_class = serializers.SupportStatsSerializer
    pagination_class = None

    def get(self, request, format=None):
        today = date.today()
        user = request.user
        issues = models.Issue.objects.all()
        broadcasts_visible = True

        if not (user.is_staff or user.is_support):
            helpdesk_ids = (
                get_provider_helpdesk_ids(user)
                if config.WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED
                else set()
            )
            if not helpdesk_ids:
                raise rf_exceptions.PermissionDenied()
            issues = issues.filter(provider_helpdesk_id__in=helpdesk_ids)
            # Broadcasts are the operator talking to their users; a provider
            # has no part in them.
            broadcasts_visible = False

        open_issues_count = issues.open().count()
        closed_this_month_count = (
            issues.closed()
            .filter(
                resolution_date__year=today.year,
                resolution_date__month=today.month,
            )
            .count()
        )

        recent_broadcasts_count = (
            BroadcastMessage.objects.filter(
                state=BroadcastMessage.States.SENT,
                created__year=today.year,
                created__month=today.month,
            ).count()
            if broadcasts_visible
            else 0
        )

        data = {
            "open_issues_count": open_issues_count,
            "closed_this_month_count": closed_this_month_count,
            "recent_broadcasts_count": recent_broadcasts_count,
        }

        return JsonResponse(data)


_WEBHOOK_SECRET_HEADER = "HTTP_X_WEBHOOK_SECRET"


def _webhook_shared_secret_check(request, constance_setting_name):
    """
    Validate the inbound webhook against a shared secret stored in
    Constance. Returns a Response on rejection, or None on success.

    Opt-in: if the operator has not configured a secret, the check is
    skipped and the request is allowed through (preserves the legacy
    unauthenticated behaviour). Once a secret is set, requests must
    carry a matching `X-Webhook-Secret` header.
    """
    expected = getattr(config, constance_setting_name, "") or ""
    if not expected:
        return None
    received = request.META.get(_WEBHOOK_SECRET_HEADER, "")
    if not received or not hmac.compare_digest(received, expected):
        logger.warning(
            "Inbound webhook rejected: invalid or missing X-Webhook-Secret for %s.",
            constance_setting_name,
        )
        return response.Response(
            {"detail": "Invalid or missing X-Webhook-Secret header."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


class WebHookReceiverView(CheckExtensionMixin, views.APIView):
    authentication_classes = ()
    permission_classes = ()
    serializer_class = serializers.WebHookReceiverSerializer

    def post(self, request):
        rejection = _webhook_shared_secret_check(request, "JIRA_WEBHOOK_SHARED_SECRET")
        if rejection is not None:
            return rejection
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
        rejection = _webhook_shared_secret_check(
            request, "ZAMMAD_WEBHOOK_SHARED_SECRET"
        )
        if rejection is not None:
            return rejection
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
        rejection = _webhook_shared_secret_check(request, "SMAX_WEBHOOK_SHARED_SECRET")
        if rejection is not None:
            return rejection
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
        from waldur_mastermind.support.backend.atlassian_discovery import (
            AtlassianDiscoveryService,
            TemporaryCredentials,
        )

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
        from waldur_mastermind.support.backend.atlassian_discovery import (
            AtlassianDiscoveryError,
        )

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
        from waldur_mastermind.support.backend.atlassian_discovery import (
            AtlassianDiscoveryError,
        )

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
        from waldur_mastermind.support.backend.atlassian_discovery import (
            AtlassianDiscoveryError,
        )

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
        from waldur_mastermind.support.backend.atlassian_discovery import (
            AtlassianDiscoveryError,
        )

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
