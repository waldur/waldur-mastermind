import json
import logging

import django_filters
import requests
from constance import config
from django.db import transaction
from django.db.models import Count, Max
from django.http import StreamingHttpResponse
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, status, viewsets
from rest_framework import exceptions as rf_exceptions
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core.filters import StaffOrUserFilter
from waldur_core.core.models import User
from waldur_core.core.views import (
    ActionsViewSet,
    ConstanceCheckExtensionMixin,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure import permissions
from waldur_mastermind.chat import models, serializers
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.parsers import StreamParser, parse_tool_call
from waldur_mastermind.chat.prompts import SYSTEM_PROMPT
from waldur_mastermind.chat.tool_executor import ToolExecutor
from waldur_mastermind.chat.tools import TOOL_REGISTRY, get_tools_prompt

logger = logging.getLogger(__name__)


class LLMConfigurationMixin(ConstanceCheckExtensionMixin):
    """
    Validates that LLM chat is enabled and properly configured.
    Extends ConstanceCheckExtensionMixin to check LLM_CHAT_ENABLED flag.
    """

    extension_name = "LLM_CHAT"

    def initial(self, request, *args, **kwargs):
        # Call parent to check LLM_CHAT_ENABLED via ConstanceCheckExtensionMixin
        super().initial(request, *args, **kwargs)

        # Validate additional API settings
        if not config.LLM_INFERENCES_API_URL:
            exc = rf_exceptions.APIException(
                _("LLM inference API URL is not configured."),
            )
            exc.status_code = status.HTTP_409_CONFLICT
            raise exc

        if not config.LLM_INFERENCES_API_TOKEN:
            exc = rf_exceptions.APIException(
                _("LLM inference API token is not configured."),
            )
            exc.status_code = status.HTTP_409_CONFLICT
            raise exc


def validate_tool_call(tool_name, user):
    """Validates if the tool exists and user is authenticated."""
    if not user or not user.is_authenticated:
        raise rf_exceptions.NotAuthenticated()

    if tool_name not in TOOL_REGISTRY:
        raise rf_exceptions.ValidationError(
            {"tool": _("Tool '%s' is not recognized." % tool_name)}
        )


class LLMStreamer:
    """
    Handles the stateful logic of streaming and buffering NDJSON responses
    from an upstream LLM provider.

    Bandwidth optimizations:
    1. NDJSON Protocol: Removes 'data:' prefix and double newlines (SSE overhead).
    2. Short Keys: Uses single-char keys ('k', 'c') to minimize payload.
    3. Flattened Structure: Merges protocol fields with data fields.
    4. Compact JSON: Removes whitespace separators.
    5. Buffered Flushing: Reduces packet count by buffering text chunks.
    """

    def __init__(
        self,
        input_text,
        url,
        token,
        user=None,
        thread=None,
        storage_enabled=False,
        original_input="",
        update_thread_name=None,
    ):
        self.url = url
        # Inject tool definitions and UI capabilities into the system prompt (system prompt is currently in external service, will be migrated fully to Waldur, once ready).
        system_prompt = SYSTEM_PROMPT.format(tools=get_tools_prompt())

        system_marker = "This is the system prompt:"

        if system_marker in input_text:
            # Inject immediately after the system prompt marker to make it part of the system instructions
            full_input = input_text.replace(
                system_marker,
                f"{system_marker}\n{system_prompt}\n",
                1,  # Replace only the first occurrence
            )
        else:
            # Fallback: Prepend to the very beginning if no system prompt marker found
            full_input = f"{system_prompt}\n\n{input_text}"

        self.payload = {"input": full_input}
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Token {token}",
        }

        self.parser = StreamParser()
        self.accumulated_content = ""  # For tool call detection
        self.user = user
        self.input_tokens = 0
        self.output_tokens = 0
        self.error = None
        self.is_tool_call = False  # Track if response looks like a tool call
        self.might_be_tool_call = False  # Track if we're buffering potential tool call

        self.thread = thread
        self.storage_enabled = storage_enabled
        self.original_input = original_input
        self.update_thread_name = update_thread_name

    def _format_ndjson(self, data: dict) -> str:
        """
        Helper to format a dict as a Newline Delimited JSON line.
        """
        return f"{json.dumps(data, separators=(',', ':'))}\n"

    def __iter__(self):
        if self.storage_enabled and self.thread:
            yield self._format_ndjson({"m": {"thread_uuid": str(self.thread.uuid)}})

        try:
            with requests.post(
                self.url,
                json=self.payload,
                headers=self.headers,
                stream=True,
                timeout=(5, 60),
            ) as response:
                response.raise_for_status()

                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue

                    line = raw_line.strip()
                    if not line.startswith("data: "):
                        logger.debug("Dropping upstream SSE line: %s", raw_line)
                        continue

                    payload_str = line[len("data: ") :]

                    try:
                        obj = json.loads(payload_str)
                    except json.JSONDecodeError:
                        logger.error("Failed to decode LLM SSE payload.", exc_info=True)
                        continue

                    content = obj.get("content")
                    metadata = obj.get("additional_kwargs")

                    if content:
                        self.accumulated_content += content

                        # Check if this looks like a tool call
                        if not self.is_tool_call and not self.might_be_tool_call:
                            stripped = self.accumulated_content.strip()

                            # Check if it could be a tool call
                            if stripped.startswith("{"):
                                # Might be a tool call - don't stream yet
                                self.might_be_tool_call = True
                                # Try to parse to see if it's complete
                                tentative_parse = parse_tool_call(stripped)
                                if tentative_parse:
                                    self.is_tool_call = True
                                # Continue buffering either way
                                continue
                        elif self.might_be_tool_call and not self.is_tool_call:
                            # Already buffering, check if we can confirm it's a tool call
                            tentative_parse = parse_tool_call(
                                self.accumulated_content.strip()
                            )
                            if tentative_parse:
                                self.is_tool_call = True
                            # Continue buffering until confirmed or stream ends
                            continue

                        # Only stream if we know it's NOT a tool call
                        if not self.is_tool_call and not self.might_be_tool_call:
                            for block in self.parser.parse(content):
                                yield self._format_ndjson(block)

                    if metadata:
                        # Extract token counts from metadata for internal tracking
                        usage = metadata.get("usage_metadata", {})
                        self.input_tokens = usage.get("input_tokens", 0)
                        self.output_tokens = usage.get("output_tokens", 0)

                # Final sweep
                for block in self.parser.flush():
                    yield self._format_ndjson(block)

                # Handle tool call or regular content
                if self.is_tool_call and self.user:
                    tool_call = parse_tool_call(self.accumulated_content)
                    if tool_call:
                        tool_name = tool_call.get("tool")
                        arguments = tool_call.get("arguments", {})

                        logger.debug(
                            "Executing tool call",
                            extra={
                                "tool_name": tool_name,
                                "user_id": self.user.id,
                            },
                        )

                        tool_executor = ToolExecutor(self.user)
                        result = tool_executor.execute_tool(tool_name, arguments)

                        # Parse tool result using StreamParser (same as markdown/code)
                        tool_block = self.parser.parse_tool_result(result)
                        if tool_block:
                            yield self._format_ndjson(tool_block)
                    else:
                        # Looked like JSON but wasn't a valid tool call - send as-is
                        yield self._format_ndjson({"c": self.accumulated_content})

        except requests.RequestException as e:
            logger.error("Upstream LLM request failed.", exc_info=True)
            self.error = str(e)
            yield self._format_ndjson(
                {"e": "Chat processing was interrupted. Please try again later."}
            )

        finally:
            # Always record usage, even if stream was interrupted (GeneratorExit)
            self._record_usage()
            self._persist_messages()
            self._apply_thread_name()

    def _persist_messages(self):
        """
        Save user and assistant messages to the thread if storage is enabled.
        Called in the finally block so the assistant message is always persisted,
        even if the stream was interrupted partway through.
        """
        if not self.storage_enabled or not self.thread:
            return

        try:
            with transaction.atomic():
                locked_thread = models.ThreadSession.objects.select_for_update().get(
                    pk=self.thread.pk
                )
                last_index = (
                    locked_thread.messages.aggregate(Max("sequence_index"))[
                        "sequence_index__max"
                    ]
                    or 0
                )

                models.Message.objects.create(
                    thread=locked_thread,
                    role=models.Message.Role.USER,
                    content=self.original_input,
                    sequence_index=last_index + 1,
                )
                models.Message.objects.create(
                    thread=locked_thread,
                    role=models.Message.Role.ASSISTANT,
                    content=self.accumulated_content,
                    sequence_index=last_index + 2,
                )
        except Exception as e:
            logger.error(
                f"Failed to persist messages for thread {self.thread.uuid}: {e}",
                exc_info=True,
            )

    def _apply_thread_name(self):
        """
        Update the target thread's name with the accumulated response.
        Used for title-generation calls: the frontend passes the main thread's
        UUID as update_thread_name, and the LLM response becomes the thread title.
        """
        if not self.update_thread_name:
            return

        title = self.accumulated_content.strip()
        if not title:
            return

        try:
            models.ThreadSession.objects.filter(uuid=self.update_thread_name).update(
                name=title[:150]
            )
        except Exception as e:
            logger.error(
                f"Failed to update thread name for {self.update_thread_name}: {e}",
                exc_info=True,
            )

    def _record_usage(self):
        """
        Atomically update token quota.
        Uses TokenQuota.for_user() for concurrent-safe updates.
        """
        if not self.user:
            return

        if self.input_tokens == 0 and self.output_tokens == 0:
            if not self.error:
                return

        try:
            with transaction.atomic():
                quota = TokenQuota.for_user(self.user, True)

                total_tokens = self.input_tokens + self.output_tokens
                quota.add_usage(total_tokens)

                logger.info(
                    f"Recorded AI usage for {self.user.username}: "
                    f"input={self.input_tokens}, output={self.output_tokens}, "
                    f"daily usage={quota.daily_usage}"
                )

        except Exception as e:
            logger.error(
                f"Failed to record AI usage for {self.user.username}: {e}",
                exc_info=True,
            )


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
            logger.error(f"Token quota configuration error: {e}")
            exc = rf_exceptions.APIException(
                _("AI Token quota system is misconfigured. Please contact support.")
            )
            exc.status_code = status.HTTP_409_CONFLICT
            raise exc

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

        thread = None
        storage_enabled = config.LLM_CHAT_STORAGE_ENABLED
        update_thread_name = serializer.validated_data.get("update_thread_name")

        if update_thread_name:
            # Title-generation call: validate ownership, skip thread creation and persistence
            if not models.ThreadSession.objects.filter(
                uuid=update_thread_name, chat_session__user=user
            ).exists():
                raise rf_exceptions.NotFound("Thread not found.")
            storage_enabled = False
        elif storage_enabled:
            thread_uuid = serializer.validated_data.get("thread_uuid")
            if thread_uuid:
                try:
                    thread = models.ThreadSession.objects.get(
                        uuid=thread_uuid, chat_session__user=user
                    )
                except models.ThreadSession.DoesNotExist:
                    raise rf_exceptions.NotFound("Thread not found.")
            else:
                session, _ = models.ChatSession.objects.get_or_create(user=user)
                thread = models.ThreadSession.objects.create(chat_session=session)

        streamer = LLMStreamer(
            input_text=serializer.validated_data["input"],
            url=config.LLM_INFERENCES_API_URL,
            token=config.LLM_INFERENCES_API_TOKEN,
            user=user,
            thread=thread,
            storage_enabled=storage_enabled,
            original_input=serializer.validated_data["input"],
            update_thread_name=update_thread_name,
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
        parameters=[serializers.TokenQuotaUsageQuerySerializer],
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


class ToolViewSet(viewsets.ViewSet):
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
    user = django_filters.UUIDFilter(field_name="chat_session__user__uuid")

    class Meta:
        model = models.ThreadSession
        fields = ["is_archived", "user"]

    def filter_queryset(self, queryset):
        if "is_archived" not in self.data:
            queryset = queryset.filter(is_archived=False)
        return super().filter_queryset(queryset)


class MessageFilter(django_filters.FilterSet):
    thread = django_filters.UUIDFilter(field_name="thread__uuid")

    class Meta:
        model = models.Message
        fields = ["thread"]


class ChatSessionViewSet(ActionsViewSet):
    """
    ViewSet for ChatSession model.
    Users can get or create their chat session.
    Staff/support can list all sessions.
    """

    queryset = models.ChatSession.objects.select_related("user").order_by("-created")
    serializer_class = serializers.ChatSessionSerializer
    filter_backends = [StaffOrUserFilter]
    lookup_field = "uuid"
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
        session, _ = models.ChatSession.objects.get_or_create(user=request.user)
        return Response(serializers.ChatSessionSerializer(session).data)


class ThreadSessionViewSet(ActionsViewSet):
    """
    ViewSet for ThreadSession model.
    Handles CRUD operations for chat threads.
    """

    queryset = models.ThreadSession.objects.all().order_by("-created")
    serializer_class = serializers.ThreadSessionSerializer
    filterset_class = ThreadSessionFilter
    lookup_field = "uuid"
    permission_classes = [IsAuthenticated, core_permissions.ActionsPermission]
    disabled_actions = ["destroy"]

    def get_queryset(self):
        """Filter threads to current user's threads; staff/support see all."""
        if self.request.user.is_staff or self.request.user.is_support:
            qs = models.ThreadSession.objects.all()
        else:
            qs = models.ThreadSession.objects.filter(
                chat_session__user=self.request.user
            )
        return (
            qs.select_related("chat_session__user")
            .annotate(message_count=Count("messages"))
            .order_by("-created")
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        _log_chat_access(
            EventType.CHAT_THREAD_ACCESSED, request, instance.chat_session.user
        )
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def perform_create(self, serializer):
        """Auto-create ChatSession if user doesn't have one."""
        session, _ = models.ChatSession.objects.get_or_create(user=self.request.user)
        serializer.save(chat_session=session)

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


class MessageViewSet(ActionsViewSet):
    """
    ViewSet for Message model.
    Handles CRUD operations for messages within threads.
    """

    queryset = models.Message.objects.all()
    serializer_class = serializers.MessageSerializer
    filterset_class = MessageFilter
    lookup_field = "uuid"
    permission_classes = [IsAuthenticated, core_permissions.ActionsPermission]
    disabled_actions = ["destroy", "update", "partial_update"]

    def get_queryset(self):
        """Filter messages to current user's; staff/support see all."""
        if self.request.user.is_staff or self.request.user.is_support:
            qs = models.Message.objects.all()
        else:
            qs = models.Message.objects.filter(
                thread__chat_session__user=self.request.user
            )
        if not self.request.query_params.get("include_history"):
            qs = qs.filter(replaced_by__isnull=True)
        return qs.select_related("thread", "replaces")

    def perform_create(self, serializer):
        """Validate thread ownership and auto-assign sequence_index."""
        thread = serializer.validated_data["thread"]
        if thread.chat_session.user != self.request.user:
            raise PermissionDenied("You can only create messages in your own threads.")

        # Auto-assign sequence_index with transaction lock to prevent race conditions
        with transaction.atomic():
            # Lock the thread to prevent concurrent sequence_index conflicts
            locked_thread = models.ThreadSession.objects.select_for_update().get(
                pk=thread.pk
            )
            last_index = locked_thread.messages.aggregate(Max("sequence_index"))[
                "sequence_index__max"
            ]
            if last_index is None:
                last_index = 0
            serializer.save(sequence_index=last_index + 1)

    @extend_schema(
        summary="Edit message",
        description="Edit a message (creates a new message with replaces reference). Only allows editing the last user message in a thread.",
        request={
            "application/json": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
            }
        },
        responses={200: serializers.MessageSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def edit(self, request, uuid=None):
        """Edit message (creates replacement)."""
        if "content" not in request.data:
            raise ValidationError({"content": "This field is required."})

        with transaction.atomic():
            # Lock the message to prevent concurrent edits
            try:
                original = models.Message.objects.select_for_update().get(
                    uuid=uuid, thread__chat_session__user=request.user
                )
            except models.Message.DoesNotExist:
                raise rf_exceptions.NotFound("Message not found.")

            # Only allow editing user messages
            if original.role != models.Message.Role.USER:
                raise ValidationError("Can only edit user messages")

            # Check if already replaced
            if original.replaced_by.exists():
                raise ValidationError("This message has already been edited")

            # Only allow editing the last user message
            last_user_msg = (
                original.thread.messages.filter(
                    role=models.Message.Role.USER, replaced_by__isnull=True
                )
                .order_by("-sequence_index")
                .first()
            )
            if original != last_user_msg:
                raise ValidationError("Can only edit the last user message")

            # Create replacement
            new_msg = models.Message.objects.create(
                thread=original.thread,
                role=original.role,
                content=request.data["content"],
                sequence_index=original.sequence_index,
                replaces=original,
            )
            return Response(serializers.MessageSerializer(new_msg).data)

    @extend_schema(
        summary="Get message edit history",
        description="Get all versions of a message (edit history).",
        responses={200: serializers.MessageSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"])
    def history(self, request, uuid=None):
        """Get all versions of a message (edit history)."""
        msg = self.get_object()
        history = models.Message.objects.filter(
            thread=msg.thread,
            sequence_index=msg.sequence_index,
            created__lte=msg.created,
        ).order_by("-created")
        return Response(serializers.MessageSerializer(history, many=True).data)
