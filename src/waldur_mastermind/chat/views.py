import logging

import django_filters
from constance import config
from django.db import transaction
from django.db.models import Case, Count, Exists, Max, OuterRef, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import decorators, status, viewsets
from rest_framework import exceptions as rf_exceptions
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from waldur_core.core import filters as core_filters
from waldur_core.core import permissions as core_permissions
from waldur_core.core.models import User
from waldur_core.core.views import (
    ActionsViewSet,
    ConstanceCheckExtensionMixin,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure import permissions
from waldur_mastermind.chat import models, serializers
from waldur_mastermind.chat.context_assembler import (
    build_context_with_intent,
    build_rejection_input,
)
from waldur_mastermind.chat.input_guards import (
    DetectionAction,
    InjectionResult,
    InputGuardResult,
    PIIResult,
    SeverityLevel,
    get_detection_service,
)
from waldur_mastermind.chat.intent_classifier import Intent
from waldur_mastermind.chat.llm_streamer import LLMStreamer, validate_tool_call
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.prompts.rejection import CANNED_REJECTION_MESSAGE_TEMPLATE
from waldur_mastermind.chat.tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


class LLMConfigurationMixin(ConstanceCheckExtensionMixin):
    """
    Validates that AI Assistant is enabled, the user has the required role,
    and the inference API is properly configured.

    AI_ASSISTANT_ENABLED (boolean) is the master on/off switch.
    AI_ASSISTANT_ENABLED_ROLES controls which user roles can access the feature.
    """

    extension_name = "AI_ASSISTANT"

    def initial(self, request, *args, **kwargs):
        # Check AI_ASSISTANT_ENABLED boolean via ConstanceCheckExtensionMixin
        super().initial(request, *args, **kwargs)

        # RBAC: check enabled roles
        enabled_roles = config.AI_ASSISTANT_ENABLED_ROLES
        user = request.user
        if enabled_roles == "disabled":
            raise rf_exceptions.PermissionDenied(_("AI Assistant is not available."))
        elif enabled_roles == "staff":
            if not user.is_staff:
                raise rf_exceptions.PermissionDenied(
                    _("AI Assistant is currently available to staff users only.")
                )
        elif enabled_roles == "staff_and_support":
            if not (user.is_staff or user.is_support):
                raise rf_exceptions.PermissionDenied(
                    _(
                        "AI Assistant is currently available to staff and support users only."
                    )
                )
        elif enabled_roles != "all":
            raise rf_exceptions.PermissionDenied(
                _("AI Assistant access is not configured correctly.")
            )

        # Validate additional API settings
        if not config.AI_ASSISTANT_API_URL:
            exc = rf_exceptions.APIException(
                _("AI Assistant API URL is not configured."),
            )
            exc.status_code = status.HTTP_409_CONFLICT
            raise exc

        if not config.AI_ASSISTANT_API_TOKEN:
            exc = rf_exceptions.APIException(
                _("AI Assistant API token is not configured."),
            )
            exc.status_code = status.HTTP_409_CONFLICT
            raise exc


QUOTA_EXCEEDED_MESSAGES = {
    "daily": _("Daily token limit exceeded."),
    "weekly": _("Weekly token limit exceeded."),
    "monthly": _("Monthly token limit exceeded."),
}


class ChatViewSet(LLMConfigurationMixin, viewsets.ViewSet):
    """
    ViewSet for streaming AI chat interactions.
    """

    permission_classes = [IsAuthenticated]

    def _check_input(self, user, input_text) -> InputGuardResult:
        """Check user input for threats (injection + PII). Returns InputGuardResult.

        Fail-closed: if anything goes wrong (including reading Constance config),
        the request is blocked with a synthetic CRITICAL result.
        """
        try:
            service = get_detection_service()
            result = service.check_user_input(input_text)
        except Exception:
            logger.exception("Input guard check failed — failing closed")
            return InputGuardResult(
                injection=InjectionResult(
                    score=1.0,
                    severity=SeverityLevel.CRITICAL,
                    action=DetectionAction.BLOCK,
                    detection_method="error_failsafe",
                ),
                # Empty PIIResult (action=ALLOW) — we don't claim PII was found,
                # only that the check failed, and we're blocking out of caution.
                pii=PIIResult(),
            )

        # Emit audit events for HIGH/CRITICAL detections
        detections = [
            ("Injection", result.injection, EventType.CHAT_INJECTION_DETECTED),
            ("PII", result.pii, EventType.CHAT_PII_DETECTED),
        ]
        for label, detection, event_type in detections:
            if detection.severity >= SeverityLevel.HIGH:
                event_logger.emit(
                    f"{label} detected in chat from {{user_username}}: severity={{severity}}, action={{action}}.",
                    event_type=event_type,
                    event_context={
                        "user": user,
                        "severity": detection.severity.value,
                        "action": detection.action.value,
                    },
                    scopes=[user],
                )

        return result

    def _validate_quota(self, user: User):
        """
        Validate token quota before streaming.
        Blocks only if the user is already at or above a limit.
        """
        try:
            with transaction.atomic():
                quota = TokenQuota.for_user(user, True)
                quota.ensure_periods_reset()

                for period in ("daily", "weekly", "monthly"):
                    remaining = quota.get_remaining(period)
                    if remaining is not None and remaining <= 0:
                        exc = rf_exceptions.APIException(
                            QUOTA_EXCEEDED_MESSAGES[period]
                        )
                        exc.status_code = status.HTTP_409_CONFLICT
                        raise exc
        except ValueError as e:
            logger.error("Token quota configuration error: %s", e)
            exc = rf_exceptions.APIException(
                _("AI Token quota system is misconfigured. Please contact support.")
            )
            exc.status_code = status.HTTP_409_CONFLICT
            raise exc

    def _resolve_thread(self, user, thread_uuid):
        """Look up an existing thread or create a new one.

        Returns (thread, is_new_thread).
        """
        if thread_uuid:
            try:
                thread = models.ThreadSession.objects.get(
                    uuid=thread_uuid, chat_session__user=user
                )
            except models.ThreadSession.DoesNotExist:
                raise rf_exceptions.NotFound("Thread not found.")

            # Clear any stale cancel flag from a previous stream that may not
            # have completed cleanly (e.g. process killed mid-stream).
            if thread.cancel_requested_at is not None:
                thread.cancel_requested_at = None
                thread.save(update_fields=["cancel_requested_at"])

            return thread, False

        session, _created = models.ChatSession.objects.get_or_create(user=user)
        thread = models.ThreadSession.objects.create(chat_session=session)
        return thread, True

    def _build_llm_prompt(self, user, thread, raw_message, detection_result):
        """Prepare the LLM prompt, canned response, PII warning, and intent.

        Returns (llm_prompt, canned_response, pii_warning, intent).
        """
        canned_response = None
        pii_warning = None
        intent = Intent.AMBIGUOUS

        if detection_result.action == DetectionAction.BLOCK:
            rejection_prompt = build_rejection_input(thread)
            if rejection_prompt:
                llm_prompt = rejection_prompt
            else:
                llm_prompt = []
                canned_response = CANNED_REJECTION_MESSAGE_TEMPLATE.format(
                    organization=config.SITE_NAME,
                )
            # Send PII-specific warning to frontend if the block involves PII
            if detection_result.pii.pii_detections:
                pii_warning = detection_result.pii.user_message
        else:
            is_redacted = detection_result.action == DetectionAction.REDACT
            user_input = (
                detection_result.pii.redacted_text if is_redacted else raw_message
            )
            result = build_context_with_intent(
                user=user,
                user_input=user_input,
                thread=thread,
            )
            llm_prompt = result.messages
            intent = result.intent
            if is_redacted or detection_result.action == DetectionAction.WARN:
                pii_warning = detection_result.pii.user_message

        return llm_prompt, canned_response, pii_warning, intent

    def _create_edit_message(
        self, locked_thread, user, edit_message_uuid, stored_content
    ):
        """Validate and create a replacement for an edited user message.

        Must be called inside transaction.atomic() with locked_thread
        already acquired via select_for_update().
        """
        try:
            original_msg = models.Message.objects.select_for_update().get(
                uuid=edit_message_uuid,
                thread=locked_thread,
                thread__chat_session__user=user,
            )
        except models.Message.DoesNotExist:
            logger.warning(
                "Edit mode: message %s not found in thread %s for user %s",
                edit_message_uuid,
                locked_thread.uuid,
                user.username,
            )
            raise rf_exceptions.NotFound("Message not found.")

        if original_msg.role != models.Message.Role.USER:
            logger.warning(
                "Edit mode: attempted to edit non-user message %s (role=%s)",
                edit_message_uuid,
                original_msg.role,
            )
            raise ValidationError(_("Can only edit user messages."))

        last_user_msg = (
            locked_thread.messages.filter(
                role=models.Message.Role.USER,
                replaced_by__isnull=True,
            )
            .order_by("-sequence_index")
            .first()
        )
        if original_msg != last_user_msg:
            logger.warning(
                "Edit mode: attempted to edit non-last user message %s in thread %s",
                edit_message_uuid,
                locked_thread.uuid,
            )
            raise ValidationError(_("Can only edit the last user message."))

        return models.Message.objects.create(
            thread=locked_thread,
            role=models.Message.Role.USER,
            blocks=[
                {
                    "id": "blk_0",
                    "key": "markdown",
                    "status": "complete",
                    "content": stored_content,
                }
            ],
            sequence_index=original_msg.sequence_index,
            replaces=original_msg,
        )

    def _persist_messages(
        self, thread, mode, edit_message_uuid, user, stored_content, detection_result
    ):
        """Pre-create user message and assistant placeholder atomically.

        For all modes the assistant placeholder is reserved upfront so a
        reconnecting client (or a rapid follow-up request) cannot claim
        the same sequence_index before the background worker persists.

        Returns (user_msg, assistant_placeholder).
        """
        if not thread:
            return None, None

        with transaction.atomic():
            locked_thread = models.ThreadSession.objects.select_for_update().get(
                pk=thread.pk
            )

            user_msg = None
            assistant_placeholder = None

            if mode == models.ChatMode.EDIT:
                user_msg = self._create_edit_message(
                    locked_thread, user, edit_message_uuid, stored_content
                )
            elif mode != models.ChatMode.RELOAD:
                # Normal mode: create user message at next index
                last_index = (
                    locked_thread.messages.aggregate(Max("sequence_index"))[
                        "sequence_index__max"
                    ]
                    or 0
                )
                user_msg = models.Message.objects.create(
                    thread=locked_thread,
                    role=models.Message.Role.USER,
                    blocks=[
                        {
                            "id": "blk_0",
                            "key": "markdown",
                            "status": "complete",
                            "content": stored_content,
                        }
                    ],
                    sequence_index=last_index + 1,
                )

            # Create assistant placeholder for every mode
            if mode in (models.ChatMode.EDIT, models.ChatMode.RELOAD):
                # Replace the last active assistant message
                last_assistant = (
                    locked_thread.messages.filter(
                        role=models.Message.Role.ASSISTANT,
                        replaced_by__isnull=True,
                    )
                    .order_by("-sequence_index")
                    .first()
                )
                if last_assistant:
                    assistant_placeholder = models.Message.objects.create(
                        thread=locked_thread,
                        role=models.Message.Role.ASSISTANT,
                        blocks=[],
                        sequence_index=last_assistant.sequence_index,
                        replaces=last_assistant,
                    )
            else:
                # Normal mode: new assistant slot at next index
                assistant_placeholder = models.Message.objects.create(
                    thread=locked_thread,
                    role=models.Message.Role.ASSISTANT,
                    blocks=[],
                    sequence_index=user_msg.sequence_index + 1,
                )

            if detection_result.is_flagged and user_msg:
                user_msg.apply_detection_result(detection_result)
                locked_thread.update_detection_flags()

        return user_msg, assistant_placeholder

    @extend_schema(
        request=serializers.ChatRequestSerializer,
        responses={
            (200, "application/x-ndjson"): serializers.ChatResponseSerializer,
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def stream(self, request):
        serializer = serializers.ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        self._validate_quota(user)

        mode = serializer.validated_data.get("mode")
        edit_message_uuid = serializer.validated_data.get("edit_message_uuid")
        raw_message = serializer.validated_data["input"]

        detection_result = self._check_input(user, raw_message)

        thread, is_new_thread = self._resolve_thread(
            user, serializer.validated_data.get("thread_uuid")
        )

        llm_prompt, canned_response, pii_warning, intent = self._build_llm_prompt(
            user, thread, raw_message, detection_result
        )

        stored_content = detection_result.pii.redacted_text or raw_message
        user_msg, assistant_placeholder = self._persist_messages(
            thread, mode, edit_message_uuid, user, stored_content, detection_result
        )

        streamer = LLMStreamer(
            messages=llm_prompt,
            url=config.AI_ASSISTANT_API_URL,
            token=config.AI_ASSISTANT_API_TOKEN,
            user=user,
            thread=thread,
            original_input=stored_content,
            is_new_thread=is_new_thread,
            mode=mode,
            user_msg=user_msg,
            assistant_msg=assistant_placeholder,
            canned_response=canned_response,
            pii_warning=pii_warning,
            intent=intent,
        )

        return StreamingHttpResponse(
            streamer,
            content_type="application/x-ndjson",
        )


class TokenQuotaViewSet(ActionsViewSet):
    """
    Access to user token quota and usage.
    Provides only custom actions with no standard CRUD operations.
    """

    queryset = TokenQuota.objects.all().order_by("-created")
    serializer_class = serializers.TokenQuotaUsageResponseSerializer
    lookup_field = "uuid"
    http_method_names = ["get", "post", "options"]  # Exclude HEAD
    disabled_actions = [
        "list",
        "retrieve",
        "create",
        "update",
        "partial_update",
        "destroy",
    ]
    permission_classes = [IsAuthenticated, core_permissions.ActionsPermission]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="user_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of user to view quota for (staff/support only). Omit to view your own quota.",
                extensions={"x-waldur-operation-id": "users_retrieve"},
            ),
        ],
        responses={200: serializers.TokenQuotaUsageResponseSerializer},
        description="""
        Get current token quota and usage for the requesting user.

        Returns token quota for all periods (daily, weekly, monthly):
        - limit: User's custom limit (null = use system default, -1 = unlimited, or positive integer)
        - usage: Tokens used in current period
        - remaining: Tokens remaining (null if unlimited)
        - reset_at: When the period resets
        - system_default: System-wide default limit from configuration (for transparency when limit is null)
        """,
    )
    @decorators.action(detail=False, methods=["get"])
    def usage(self, request):
        user = request.user

        # Permission check: users can only view their own usage
        if "user_uuid" in request.query_params:
            requested_user_uuid = request.query_params.get("user_uuid")
            if str(user.uuid) != requested_user_uuid and not (
                user.is_staff or user.is_support
            ):
                raise rf_exceptions.PermissionDenied(
                    "You can only view your own usage."
                )
            # Allow staff/support to view any user
            try:
                user = User.objects.get(uuid=requested_user_uuid)
            except User.DoesNotExist:
                raise rf_exceptions.NotFound("User not found.")

        quota = TokenQuota.for_user(user)
        serializer = serializers.TokenQuotaUsageResponseSerializer(quota)
        return Response(serializer.data)

    @extend_schema(
        summary="Set token quota for user",
        description=(
            "Allows staff/support to set token quota limits for a specific user. "
            "Configure daily, weekly, and monthly limits:\n"
            "- Omit field or send `null`: Use system default\n"
            "- `-1`: Unlimited (no quota enforcement)\n"
            "- `0` or positive integer: Specific token limit"
        ),
        responses={200: None},
    )
    @decorators.action(detail=False, methods=["post"])
    def set_quota(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_uuid = serializer.validated_data["user_uuid"]

        # Validate user exists
        try:
            user = User.objects.get(uuid=user_uuid)
        except User.DoesNotExist:
            raise rf_exceptions.NotFound("User not found.")

        # Get or create TokenQuota for the user
        quota = TokenQuota.for_user(user)

        # Update limits only if provided in request
        if "daily_limit" in serializer.validated_data:
            quota.daily_limit = serializer.validated_data["daily_limit"]
        if "weekly_limit" in serializer.validated_data:
            quota.weekly_limit = serializer.validated_data["weekly_limit"]
        if "monthly_limit" in serializer.validated_data:
            quota.monthly_limit = serializer.validated_data["monthly_limit"]

        quota.save()

        return Response(status=status.HTTP_200_OK)

    set_quota_permissions = [permissions.is_staff_or_support]
    set_quota_serializer_class = serializers.SetTokenQuotaSerializer


class ToolViewSet(LLMConfigurationMixin, viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=serializers.ToolExecuteSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    @decorators.action(detail=False, methods=["post"], url_path="execute")
    def execute_tool(self, request):
        """Execute a tool and return the result."""
        serializer = serializers.ToolExecuteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tool_name = serializer.validated_data["tool"]
        arguments = serializer.validated_data["arguments"]

        validate_tool_call(tool_name, request.user)

        tool_executor = ToolExecutor(request.user)
        result = tool_executor.execute_tool(tool_name, arguments)

        # ToolExecutor returns {"type": "error", ...} for injection blocks and other errors
        if result.get("type") == "error":
            return Response(
                {"detail": result.get("error", "Unable to process this request.")},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(result, status=status.HTTP_200_OK)


def _log_chat_access(event_type, request, target_user):
    """Emit an audit event when staff/support accesses another user's chat data."""
    if request.user == target_user:
        return
    event_logger.emit(
        "Chat data of {target_username} accessed by {accessor_username}.",
        event_type=event_type,
        event_context={
            "accessor_username": request.user.username,
            "target_username": target_user.username,
        },
        scopes=[target_user],
    )


class ThreadSessionFilter(django_filters.FilterSet):
    user = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="chat_session__user__uuid"
    )
    created = django_filters.DateFilter(field_name="created", lookup_expr="date")
    modified = django_filters.DateFilter(field_name="modified", lookup_expr="date")
    query = django_filters.CharFilter(method="filter_by_query")
    is_flagged = django_filters.BooleanFilter(method="filter_is_flagged")
    has_feedback = django_filters.BooleanFilter(method="filter_has_feedback")
    max_severity = django_filters.ChoiceFilter(
        choices=[(s.value, s.value.title()) for s in SeverityLevel],
        method="filter_max_severity",
    )
    input_tokens_min = django_filters.NumberFilter(
        field_name="input_tokens", lookup_expr="gte"
    )
    input_tokens_max = django_filters.NumberFilter(
        field_name="input_tokens", lookup_expr="lte"
    )
    output_tokens_min = django_filters.NumberFilter(
        field_name="output_tokens", lookup_expr="gte"
    )
    output_tokens_max = django_filters.NumberFilter(
        field_name="output_tokens", lookup_expr="lte"
    )
    total_tokens_min = django_filters.NumberFilter(
        field_name="total_tokens", lookup_expr="gte"
    )
    total_tokens_max = django_filters.NumberFilter(
        field_name="total_tokens", lookup_expr="lte"
    )
    scope = django_filters.ChoiceFilter(
        choices=[("own", "Own")],
        method="filter_scope",
    )
    o = django_filters.OrderingFilter(
        fields=(
            "created",
            "modified",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        )
    )

    class Meta:
        model = models.ThreadSession
        fields = ["is_archived", "user"]

    def filter_has_feedback(self, queryset, name, value):
        return queryset.filter(has_feedback=value)

    def filter_is_flagged(self, queryset, name, value):
        if value:
            return queryset.filter(flags__contains={"is_flagged": True})
        return queryset.exclude(flags__contains={"is_flagged": True})

    def filter_max_severity(self, queryset, name, value):
        severity = SeverityLevel(value)
        if severity == SeverityLevel.NONE:
            return queryset.exclude(flags__contains={"is_flagged": True})
        return queryset.filter(flags__max_severity=value)

    def filter_by_query(self, queryset, name, value):
        """Full-text search across thread name and user details."""
        return queryset.filter(
            Q(name__icontains=value)
            | Q(chat_session__user__username__icontains=value)
            | Q(chat_session__user__first_name__icontains=value)
            | Q(chat_session__user__last_name__icontains=value)
            | Q(chat_session__user__email__icontains=value)
        ).distinct()

    def filter_scope(self, queryset, name, value):
        if value == "own":
            return queryset.filter(chat_session__user=self.request.user)
        return queryset


class MessageFilter(django_filters.FilterSet):
    thread = core_filters.RelatedUUIDFilter(
        view_name="chat-thread-detail", field_name="thread__uuid"
    )
    include_history = django_filters.BooleanFilter(method="filter_include_history")
    is_flagged = django_filters.BooleanFilter()
    feedback_score = django_filters.BooleanFilter()

    def filter_include_history(self, queryset, name, value):
        if not value:
            return queryset.filter(replaced_by__isnull=True)
        return queryset

    @property
    def qs(self):
        parent_qs = super().qs
        # Default: exclude replaced messages when include_history is not provided
        if "include_history" not in self.data:
            parent_qs = parent_qs.filter(replaced_by__isnull=True)
        return parent_qs

    class Meta:
        model = models.Message
        fields = ["thread", "is_flagged", "feedback_score"]


class ChatSessionViewSet(LLMConfigurationMixin, ActionsViewSet):
    """
    ViewSet for ChatSession model.
    Users can get or create their chat session.
    Staff/support can list all sessions.
    """

    queryset = models.ChatSession.objects.select_related("user").order_by("-created")
    serializer_class = serializers.ChatSessionSerializer
    filter_backends = [core_filters.StaffOrUserFilter]
    lookup_field = "uuid"
    http_method_names = ["get", "options"]
    permission_classes = [IsAuthenticated, core_permissions.ActionsPermission]
    disabled_actions = ["create", "update", "partial_update", "destroy"]

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        _log_chat_access(EventType.CHAT_SESSION_ACCESSED, request, instance.user)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @extend_schema(
        summary="Get or create current user's chat session",
        description="Returns the current user's chat session, creating it if it doesn't exist.",
        responses={200: serializers.ChatSessionSerializer},
    )
    @decorators.action(detail=False, methods=["get"])
    def current(self, request):
        """Get or create current user's chat session."""
        session, _created = models.ChatSession.objects.get_or_create(user=request.user)
        return Response(serializers.ChatSessionSerializer(session).data)


class ThreadSessionViewSet(LLMConfigurationMixin, ActionsViewSet):
    """
    ViewSet for ThreadSession model.
    Provides read-only access and archive/unarchive actions for chat threads.
    Staff and support users can view all threads; regular users see only their own.
    """

    queryset = models.ThreadSession.objects.all().order_by("-created")
    serializer_class = serializers.ThreadSessionSerializer
    lookup_field = "uuid"
    http_method_names = ["get", "post", "options"]
    permission_classes = [IsAuthenticated, core_permissions.ActionsPermission]
    disabled_actions = ["create", "destroy", "update", "partial_update"]
    filterset_class = ThreadSessionFilter

    def get_queryset(self):
        """Filter threads to current user's threads; staff/support see all."""
        qs = models.ThreadSession.objects.all()

        if not (self.request.user.is_staff or self.request.user.is_support):
            qs = qs.filter(chat_session__user=self.request.user)

        return (
            qs.select_related("chat_session__user")
            .annotate(
                message_count=Count("messages"),
                _msg_input=Sum("messages__input_tokens"),
                _msg_output=Sum("messages__output_tokens"),
            )
            .annotate(
                input_tokens=Case(
                    When(
                        _msg_input__isnull=True,
                        title_gen_input_tokens__isnull=True,
                        then=None,
                    ),
                    default=Coalesce("_msg_input", Value(0))
                    + Coalesce("title_gen_input_tokens", Value(0)),
                ),
                output_tokens=Case(
                    When(
                        _msg_output__isnull=True,
                        title_gen_output_tokens__isnull=True,
                        then=None,
                    ),
                    default=Coalesce("_msg_output", Value(0))
                    + Coalesce("title_gen_output_tokens", Value(0)),
                ),
            )
            .annotate(
                total_tokens=Case(
                    When(
                        input_tokens__isnull=True,
                        output_tokens__isnull=True,
                        then=None,
                    ),
                    default=Coalesce("input_tokens", Value(0))
                    + Coalesce("output_tokens", Value(0)),
                ),
            )
            .annotate(
                has_feedback=Exists(
                    models.Message.objects.filter(
                        thread=OuterRef("pk"),
                        feedback_score__isnull=False,
                    )
                )
            )
            .order_by("-created")
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        _log_chat_access(
            EventType.CHAT_THREAD_ACCESSED, request, instance.chat_session.user
        )
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @extend_schema(
        summary="Archive thread",
        description="Archive a thread (soft delete).",
        responses={204: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        """Archive thread (soft delete)."""
        thread = self.get_object()
        thread.is_archived = True
        thread.save(update_fields=["is_archived"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Unarchive thread",
        description="Restore an archived thread.",
        responses={204: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def unarchive(self, request, uuid=None):
        """Restore archived thread."""
        thread = self.get_object()
        thread.is_archived = False
        thread.save(update_fields=["is_archived"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Cancel active stream",
        description="Request cancellation of the active LLM stream for this thread.",
        responses={200: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        """Request cancellation of the active LLM stream.

        Sets a DB flag that the streaming worker polls. Works across
        gunicorn workers because the flag lives in PostgreSQL.  The
        flag is idempotent and cleared automatically when the worker
        finishes, so fire-and-forget from the frontend is safe.
        """
        thread = get_object_or_404(
            models.ThreadSession.objects.filter(chat_session__user=request.user),
            uuid=uuid,
        )
        thread.cancel_requested_at = timezone.now()
        thread.save(update_fields=["cancel_requested_at"])
        logger.info(
            "Cancel requested for thread %s by user %s",
            thread.uuid,
            request.user.username,
        )
        return Response(status=status.HTTP_200_OK)


class MessageViewSet(LLMConfigurationMixin, ActionsViewSet):
    """
    ViewSet for Message model.
    Provides read-only list access for chat messages.
    Staff and support users can view all messages; regular users see only their own.
    """

    queryset = models.Message.objects.all()
    serializer_class = serializers.MessageSerializer
    filterset_class = MessageFilter
    lookup_field = "uuid"
    pagination_class = None  # Messages are thread-scoped; return all in one response
    http_method_names = ["get", "post", "options"]
    permission_classes = [IsAuthenticated, core_permissions.ActionsPermission]
    disabled_actions = ["create", "destroy", "update", "partial_update", "retrieve"]

    # Feedback permissions are enforced by thread__chat_session__user filter
    feedback_serializer_class = serializers.MessageFeedbackSerializer

    def get_queryset(self):
        """Filter messages to current user's; staff/support see all."""
        if self.request.user.is_staff or self.request.user.is_support:
            qs = models.Message.objects.all()
        else:
            qs = models.Message.objects.filter(
                thread__chat_session__user=self.request.user
            )
        return qs.select_related("thread", "replaces")

    @extend_schema(
        summary="Submit or update feedback for an assistant message",
        request=serializers.MessageFeedbackSerializer,
        responses={200: serializers.MessageSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def feedback(self, request, uuid=None):
        serializer = serializers.MessageFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            # Lock only the Message row (of=("self",)) so the JOIN on
            # thread__chat_session__user doesn't try to lock joined tables —
            # PostgreSQL rejects FOR UPDATE against outer joins.
            message = get_object_or_404(
                models.Message.objects.select_for_update(of=("self",)).filter(
                    thread__chat_session__user=request.user,
                ),
                uuid=uuid,
            )

            if message.role != models.Message.Role.ASSISTANT:
                raise ValidationError(
                    _("Feedback can only be given on assistant messages.")
                )

            message.feedback_score = serializer.validated_data["score"]
            message.feedback_comment = serializer.validated_data.get("comment")
            if serializer.validated_data["score"]:
                message.feedback_category = None
            else:
                message.feedback_category = serializer.validated_data.get("category")
            message.feedback_submitted_at = timezone.now()
            message.save(
                update_fields=[
                    "feedback_score",
                    "feedback_comment",
                    "feedback_category",
                    "feedback_submitted_at",
                ]
            )

            event_logger.emit(
                "Chat feedback submitted by {user_username}: score={score}.",
                event_type=EventType.CHAT_FEEDBACK_SUBMITTED,
                event_context={
                    "user": request.user,
                    "score": message.feedback_score,
                    "category": message.feedback_category,
                    "has_comment": bool(message.feedback_comment),
                    "message_uuid": message.uuid.hex,
                    "thread_uuid": message.thread.uuid.hex,
                },
                scopes=[request.user],
            )

        return Response(
            serializers.MessageSerializer(message, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )
