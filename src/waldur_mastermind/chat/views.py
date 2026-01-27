import json
import logging

import requests
from constance import config
from django.db import transaction
from django.http import StreamingHttpResponse
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, status, viewsets
from rest_framework import exceptions as rf_exceptions
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core.models import User
from waldur_core.core.views import (
    ActionsViewSet,
    ConstanceCheckExtensionMixin,
)
from waldur_core.structure import permissions
from waldur_mastermind.chat import serializers
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

    def __init__(self, input_text, url, token, user=None):
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

    def _format_ndjson(self, data: dict) -> str:
        """
        Helper to format a dict as a Newline Delimited JSON line.
        """
        return f"{json.dumps(data, separators=(',', ':'))}\n"

    def __iter__(self):
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

        streamer = LLMStreamer(
            input_text=serializer.validated_data["input"],
            url=config.LLM_INFERENCES_API_URL,
            token=config.LLM_INFERENCES_API_TOKEN,
            user=request.user,
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
