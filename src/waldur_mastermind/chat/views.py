import json
import logging

import requests
from constance import config
from django.http import StreamingHttpResponse
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions as rf_exceptions
from rest_framework import status, viewsets
from rest_framework.decorators import action

from waldur_core.core.exceptions import ExtensionDisabled
from waldur_mastermind.chat import serializers
from waldur_mastermind.chat.parsers import StreamParser

logger = logging.getLogger(__name__)


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

    def __init__(self, input_text, url, token):
        self.url = url
        self.payload = {"input": input_text}
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Token {token}",
        }
        self.parser = StreamParser()

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
        )

        return StreamingHttpResponse(
            streamer,
            content_type="application/x-ndjson",
        )
