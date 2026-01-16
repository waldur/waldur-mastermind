import json
import logging
import re

import requests
from constance import config
from django.http import StreamingHttpResponse
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions as rf_exceptions
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core.exceptions import ExtensionDisabled
from waldur_mastermind.chat import serializers
from waldur_mastermind.chat.parsers import StreamParser
from waldur_mastermind.chat.tool_executor import ToolExecutor
from waldur_mastermind.chat.tools import TOOL_REGISTRY, get_tools_prompt

logger = logging.getLogger(__name__)

TOOL_INSTRUCTIONS = """{tools}

RULES FOR TOOL USAGE:
1. ONLY use a tool when the user EXPLICITLY asks for it (e.g., "show my resources", "list my resources")
2. Do NOT use tools for greetings, general questions, or casual conversation
3. When using a tool, respond with ONLY the JSON object - no other text
4. Format: {{"tool": "show_user_resources", "arguments": {{}}}}
5. NEVER mention tools to the user - do not suggest using tools or explain that tools exist
"""


def parse_tool_call(content):
    """Try to parse a tool call from LLM response content."""
    content = content.strip()
    # Strip markdown code blocks if present
    if content.startswith("```"):
        content = re.sub(r"^```\w*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

    content = content.strip()
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "tool" in data:
            return data
    except json.JSONDecodeError:
        pass
    return None


class LLMConfigurationMixin:
    """
    Validates that LLM chat is enabled and properly configured.
    """

    def initial(self, request, *args, **kwargs):
        if not config.LLM_CHAT_ENABLED:
            raise ExtensionDisabled()

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

        return super().initial(request, *args, **kwargs)


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
        # Inject tool definitions into the system prompt (system prompt is currently in external service, will be migrated fully to Waldur, once ready).
        tool_instructions = TOOL_INSTRUCTIONS.format(tools=get_tools_prompt())

        system_marker = "This is the system prompt:"

        if system_marker in input_text:
            # Inject immediately after the system prompt marker to make it part of the system instructions
            full_input = input_text.replace(
                system_marker,
                f"{system_marker}\n{tool_instructions}\n",
                1,  # Replace only the first occurrence
            )
        else:
            # Fallback: Prepend to the very beginning if no system prompt marker found
            full_input = f"{tool_instructions}\n\n{input_text}"

        self.payload = {"input": full_input}
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Token {token}",
        }

        self.parser = StreamParser()
        self.accumulated_content = ""  # For tool call detection
        self.user = user
        self.is_tool_call = False  # Track if response looks like a tool call

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

                        # Check if this looks like a tool call (starts with {)
                        if (
                            not self.is_tool_call
                            and self.accumulated_content.strip().startswith("{")
                        ):
                            self.is_tool_call = True

                        if not self.is_tool_call:
                            for block in self.parser.parse(content):
                                yield self._format_ndjson(block)

                    if metadata:
                        # Flush any pending text
                        for block in self.parser.flush():
                            yield self._format_ndjson(block)

                        yield self._format_ndjson({"m": metadata})

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

                        # Send summary as markdown content
                        summary = result.get("summary", "Tool executed successfully.")
                        yield self._format_ndjson({"c": summary, "k": "markdown"})
                    else:
                        # Looked like JSON but wasn't a valid tool call - send as-is
                        yield self._format_ndjson({"c": self.accumulated_content})

        except requests.RequestException:
            logger.error("Upstream LLM request failed.", exc_info=True)
            yield self._format_ndjson(
                {"e": "Chat processing was interrupted. Please try again later."}
            )


class ChatViewSet(LLMConfigurationMixin, viewsets.ViewSet):
    serializer_class = serializers.ChatRequestSerializer

    @extend_schema(
        request=serializers.ChatRequestSerializer,
        responses={
            (200, "application/x-ndjson"): serializers.ChatResponseSerializer,
        },
    )
    @action(detail=False, methods=["post"])
    def stream(self, request):
        serializer = serializers.ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

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

    @extend_schema(
        request=None,
        responses={200: OpenApiTypes.STR},
    )
    @action(detail=False, methods=["post"])
    def invoke(self, request):
        return Response("Invoke chat response", status=status.HTTP_200_OK)


class ToolViewSet(viewsets.ViewSet):
    @extend_schema(
        request=serializers.ToolExecuteSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["post"], url_path="execute")
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
