import json
import logging

import django_filters
import requests
from constance import config
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import StreamingHttpResponse
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
    build_context,
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
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.parsers import StreamParser, parse_tool_call
from waldur_mastermind.chat.prompts import (
    CANNED_REJECTION_MESSAGE,
    TITLE_GENERATION_PROMPT,
)
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.registry import tool_registry

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

    if tool_name not in tool_registry:
        raise rf_exceptions.ValidationError(
            {
                "tool": _("Tool '%(tool_name)s' is not recognized.")
                % {"tool_name": tool_name}
            }
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
        llm_prompt,
        url,
        token,
        user=None,
        thread=None,
        original_input="",
        is_new_thread=False,
        mode=None,
        user_msg=None,
        canned_response=None,
        pii_warning=None,
    ):
        self.url = url
        self.payload = {"input": llm_prompt}
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
        self.original_input = original_input
        self.is_new_thread = is_new_thread
        self.mode = mode
        self.user_msg = user_msg
        self._persisted_message_meta = None
        self._messages_persisted = False
        self.canned_response = canned_response
        self.pii_warning = pii_warning

    def _format_ndjson(self, data: dict) -> str:
        """
        Helper to format a dict as a Newline Delimited JSON line.
        """
        return f"{json.dumps(data, separators=(',', ':'))}\n"

    def _handle_stream_block(self, block: dict):
        """
        Yield formatted NDJSON for a parser block, intercepting tool call sentinels.

        StreamParser signals embedded tool calls (LLM used a ```json code block
        instead of raw JSON) via {"_tool_call": {...}}. We execute the tool here
        so the result renders correctly instead of exposing raw JSON to the user.
        """
        if "_tool_call" in block and self.user:
            tool_data = block["_tool_call"]
            logger.debug(
                "Intercepted embedded tool call in json code block: %s",
                tool_data.get("tool"),
            )
            tool_executor = ToolExecutor(self.user)
            result = tool_executor.execute_tool(
                tool_data["tool"], tool_data.get("arguments", {})
            )
            tool_block = self.parser.parse_tool_result(result)
            if tool_block:
                yield self._format_ndjson(tool_block)
        else:
            yield self._format_ndjson(block)

    @staticmethod
    def _iter_sse_events(response):
        """Yield (content, metadata) tuples from an upstream SSE response."""
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data: "):
                logger.debug("Dropping upstream SSE line: %s", raw_line)
                continue
            try:
                obj = json.loads(line[len("data: ") :])
            except json.JSONDecodeError:
                logger.error("Failed to decode LLM SSE payload.", exc_info=True)
                continue
            yield obj.get("content"), obj.get("additional_kwargs")

    def __iter__(self):
        if self.thread:
            yield self._format_ndjson({"m": {"thread_uuid": str(self.thread.uuid)}})

        # Yield PII warning as first content event (before LLM content)
        if self.pii_warning:
            yield self._format_ndjson({"w": self.pii_warning})

        self._messages_persisted = False

        try:
            # Blocked input: stream canned rejection, persist, and skip the LLM call
            if self.canned_response:
                self.accumulated_content = self.canned_response
                for block in self.parser.parse(self.canned_response):
                    yield self._format_ndjson(block)
                for block in self.parser.flush():
                    yield self._format_ndjson(block)
                self._persist_messages()
                if self._persisted_message_meta:
                    yield self._format_ndjson({"m": self._persisted_message_meta})
                self._generate_thread_name()
                return

            with requests.post(
                self.url,
                json=self.payload,
                headers=self.headers,
                stream=True,
                timeout=(5, 60),
            ) as response:
                response.raise_for_status()

                for content, metadata in self._iter_sse_events(response):
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
                                yield from self._handle_stream_block(block)

                    if metadata:
                        usage = metadata.get("usage_metadata", {})
                        self.input_tokens = usage.get("input_tokens", 0)
                        self.output_tokens = usage.get("output_tokens", 0)

                # Final sweep
                for block in self.parser.flush():
                    yield from self._handle_stream_block(block)

                # Send buffered content that wasn't a confirmed tool call
                if self.might_be_tool_call and not self.is_tool_call:
                    yield self._format_ndjson({"c": self.accumulated_content})

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

            # Normal completion: persist and yield UUIDs
            self._persist_messages()
            if self._persisted_message_meta:
                yield self._format_ndjson({"m": self._persisted_message_meta})
            self._generate_thread_name()

        except requests.RequestException as e:
            logger.error("Upstream LLM request failed.", exc_info=True)
            self.error = str(e)
            yield self._format_ndjson(
                {"e": "Chat processing was interrupted. Please try again later."}
            )
            # Error path: persist and yield UUIDs
            self._persist_messages()
            if self._persisted_message_meta:
                yield self._format_ndjson({"m": self._persisted_message_meta})

        finally:
            # Always record usage, even if stream was interrupted (GeneratorExit)
            self._record_usage()
            # Safety net for GeneratorExit - can't yield here
            if not self._messages_persisted:
                self._persist_messages()

    def _persist_messages(self):
        """
        Save user and assistant messages to the thread.
        In reload/edit mode, replace the last assistant message.
        In edit mode, user message was pre-created in stream().
        """
        if not self.thread:
            return

        try:
            with transaction.atomic():
                locked_thread = models.ThreadSession.objects.select_for_update().get(
                    pk=self.thread.pk
                )

                persisted_user_msg = None
                persisted_assistant_msg = None
                effective_mode = self.mode

                if effective_mode in (models.ChatMode.RELOAD, models.ChatMode.EDIT):
                    # For EDIT mode, user message was pre-created in stream()
                    if effective_mode == models.ChatMode.EDIT and self.user_msg:
                        persisted_user_msg = self.user_msg

                    # Find last active assistant message to replace
                    last_assistant = (
                        locked_thread.messages.filter(
                            role=models.Message.Role.ASSISTANT,
                            replaced_by__isnull=True,
                        )
                        .order_by("-sequence_index")
                        .first()
                    )

                    if last_assistant:
                        # Create replacement with same sequence_index
                        persisted_assistant_msg = models.Message.objects.create(
                            thread=locked_thread,
                            role=models.Message.Role.ASSISTANT,
                            content=self.accumulated_content,
                            sequence_index=last_assistant.sequence_index,
                            replaces=last_assistant,
                        )
                    else:
                        # Fallback to normal mode if no assistant message found
                        logger.warning(
                            "%s mode requested but no assistant message found in thread %s, falling back to normal mode",
                            effective_mode,
                            self.thread.uuid,
                        )
                        effective_mode = None

                # Normal mode (or fallback from reload/edit)
                if effective_mode not in (models.ChatMode.RELOAD, models.ChatMode.EDIT):
                    if self.user_msg:
                        # User message was pre-created in the view
                        persisted_user_msg = self.user_msg
                    else:
                        last_index = (
                            locked_thread.messages.aggregate(Max("sequence_index"))[
                                "sequence_index__max"
                            ]
                            or 0
                        )
                        persisted_user_msg = models.Message.objects.create(
                            thread=locked_thread,
                            role=models.Message.Role.USER,
                            content=self.original_input,
                            sequence_index=last_index + 1,
                        )

                    persisted_assistant_msg = models.Message.objects.create(
                        thread=locked_thread,
                        role=models.Message.Role.ASSISTANT,
                        content=self.accumulated_content,
                        sequence_index=persisted_user_msg.sequence_index + 1,
                    )

                # Store UUIDs for metadata response
                self._persisted_message_meta = {}
                if persisted_user_msg:
                    self._persisted_message_meta["user_message_uuid"] = str(
                        persisted_user_msg.uuid
                    )
                if persisted_assistant_msg:
                    self._persisted_message_meta["assistant_message_uuid"] = str(
                        persisted_assistant_msg.uuid
                    )

                # Update thread's modified timestamp to reflect latest message
                locked_thread.save(update_fields=["modified"])

                self._messages_persisted = True

        except Exception as e:
            logger.error(
                "Failed to persist messages for thread %s: %s",
                self.thread.uuid,
                e,
                exc_info=True,
            )

    def _generate_thread_name(self):
        """
        Generate a short title for a new thread via a second LLM call.
        Updates the thread name in DB. Failures are logged but never break
        the main response.
        """
        if not self.is_new_thread or not self.thread or not self.original_input:
            return

        try:
            prompt = TITLE_GENERATION_PROMPT + self.original_input[:500]
            title_parts = []
            title_input_tokens = 0
            title_output_tokens = 0

            with requests.post(
                self.url,
                json={"input": prompt},
                headers=self.headers,
                stream=True,
                timeout=(5, 30),
            ) as resp:
                resp.raise_for_status()

                for content, metadata in self._iter_sse_events(resp):
                    if content:
                        title_parts.append(content)
                    if metadata:
                        usage = metadata.get("usage_metadata", {})
                        title_input_tokens = usage.get("input_tokens", 0)
                        title_output_tokens = usage.get("output_tokens", 0)

            self.input_tokens += title_input_tokens
            self.output_tokens += title_output_tokens

            title = "".join(title_parts).strip().strip("\"'")
            if title:
                models.ThreadSession.objects.filter(pk=self.thread.pk).update(
                    name=title[:150]
                )

        except Exception:
            logger.exception("Failed to generate thread title for %s", self.thread.uuid)

    def _record_usage(self):
        """
        Atomically update token quota.
        Uses TokenQuota.for_user() for concurrent-safe updates.
        """
        if not self.user:
            return

        # Skip recording if no tokens were exchanged and no error occurred.
        # On error, we still record a zero-usage entry for audit visibility.
        if self.input_tokens == 0 and self.output_tokens == 0 and not self.error:
            return

        try:
            with transaction.atomic():
                quota = TokenQuota.for_user(self.user, True)

                total_tokens = self.input_tokens + self.output_tokens
                quota.add_usage(total_tokens)

                logger.info(
                    "Recorded AI usage for %s: input=%d, output=%d, daily usage=%d",
                    self.user.username,
                    self.input_tokens,
                    self.output_tokens,
                    quota.daily_usage,
                )

        except Exception as e:
            logger.error(
                "Failed to record AI usage for %s: %s",
                self.user.username,
                e,
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

        # Prompt injection detection (between quota check and streaming)
        detection_result = self._check_input(user, serializer.validated_data["input"])
        is_blocked = detection_result.action == DetectionAction.BLOCK
        is_redacted = detection_result.action == DetectionAction.REDACT
        is_warned = detection_result.action == DetectionAction.WARN

        is_new_thread = False
        thread_uuid = serializer.validated_data.get("thread_uuid")
        if thread_uuid:
            try:
                thread = models.ThreadSession.objects.get(
                    uuid=thread_uuid, chat_session__user=user
                )
            except models.ThreadSession.DoesNotExist:
                raise rf_exceptions.NotFound("Thread not found.")
        else:
            session, _created = models.ChatSession.objects.get_or_create(user=user)
            thread = models.ThreadSession.objects.create(chat_session=session)
            is_new_thread = True

        # For blocked input, try context-aware LLM rejection first, fall back to static
        raw_message = serializer.validated_data["input"]
        canned_response = None
        pii_warning = None

        if is_blocked:
            rejection_prompt = build_rejection_input(thread)
            if rejection_prompt:
                llm_prompt = rejection_prompt
            else:
                llm_prompt = ""
                canned_response = CANNED_REJECTION_MESSAGE
            # Send PII-specific warning to frontend if the block involves PII
            if detection_result.pii.pii_detections:
                pii_warning = detection_result.pii.user_message
        else:
            # Use redacted text when PII was redacted, original text otherwise
            user_input = (
                detection_result.pii.redacted_text if is_redacted else raw_message
            )
            llm_prompt = build_context(
                user=user,
                user_input=user_input,
                thread=thread,
            )
            if is_redacted or is_warned:
                pii_warning = detection_result.pii.user_message

        # Pre-create user message so it persists even if the client disconnects
        user_msg = None
        # Use redacted/blocked text from PII result if available, else raw
        stored_content = detection_result.pii.redacted_text or raw_message
        if thread and mode != models.ChatMode.RELOAD:
            with transaction.atomic():
                locked_thread = models.ThreadSession.objects.select_for_update().get(
                    pk=thread.pk
                )

                if mode == models.ChatMode.EDIT:
                    # Lock and validate the target message
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

                    # Create replacement at same sequence_index
                    user_msg = models.Message.objects.create(
                        thread=locked_thread,
                        role=models.Message.Role.USER,
                        content=stored_content,
                        sequence_index=original_msg.sequence_index,
                        replaces=original_msg,
                    )
                else:
                    # Normal mode: create new user message at next index
                    last_index = (
                        locked_thread.messages.aggregate(Max("sequence_index"))[
                            "sequence_index__max"
                        ]
                        or 0
                    )
                    user_msg = models.Message.objects.create(
                        thread=locked_thread,
                        role=models.Message.Role.USER,
                        content=stored_content,
                        sequence_index=last_index + 1,
                    )

                if detection_result.is_flagged:
                    user_msg.apply_detection_result(detection_result)
                    locked_thread.update_detection_flags()

        streamer = LLMStreamer(
            llm_prompt=llm_prompt,
            url=config.LLM_INFERENCES_API_URL,
            token=config.LLM_INFERENCES_API_TOKEN,
            user=user,
            thread=thread,
            original_input=stored_content,
            is_new_thread=is_new_thread,
            mode=mode,
            user_msg=user_msg,
            canned_response=canned_response,
            pii_warning=pii_warning,
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
    max_severity = django_filters.ChoiceFilter(
        choices=[(s.value, s.value.title()) for s in SeverityLevel],
        method="filter_max_severity",
    )
    o = django_filters.OrderingFilter(fields=("created", "modified"))

    class Meta:
        model = models.ThreadSession
        fields = ["is_archived", "user"]

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


class MessageFilter(django_filters.FilterSet):
    thread = core_filters.RelatedUUIDFilter(
        view_name="chat-thread-detail", field_name="thread__uuid"
    )
    include_history = django_filters.BooleanFilter(method="filter_include_history")
    is_flagged = django_filters.BooleanFilter()

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
        fields = ["thread", "is_flagged"]


class ChatSessionViewSet(ActionsViewSet):
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


class ThreadSessionViewSet(ActionsViewSet):
    """
    ViewSet for ThreadSession model.
    Provides read-only access and archive/unarchive actions for chat threads.
    Staff and support users can view all threads; regular users see only their own.
    """

    queryset = models.ThreadSession.objects.all().order_by("-created")
    serializer_class = serializers.ThreadSessionSerializer
    filterset_class = ThreadSessionFilter
    lookup_field = "uuid"
    http_method_names = ["get", "post", "options"]
    permission_classes = [IsAuthenticated, core_permissions.ActionsPermission]
    disabled_actions = ["create", "destroy", "update", "partial_update"]

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

    def get_queryset(self):
        """Filter messages to current user's; staff/support see all."""
        if self.request.user.is_staff or self.request.user.is_support:
            qs = models.Message.objects.all()
        else:
            qs = models.Message.objects.filter(
                thread__chat_session__user=self.request.user
            )
        return qs.select_related("thread", "replaces")
