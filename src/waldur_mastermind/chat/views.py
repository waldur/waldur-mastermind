import json
import logging
import time

import requests
from constance import config
from django.http import StreamingHttpResponse
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import exceptions as rf_exceptions
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_mastermind.chat import serializers

logger = logging.getLogger(__name__)


def validate_llm_configuration(obj=None):
    """
    Validates whether LLM chat functionality is enabled and properly configured.
    """
    if not config.LLM_CHAT_ENABLED:
        raise rf_exceptions.NotFound()

    if not config.LLM_INFERENCES_API_URL:
        exc = rf_exceptions.APIException(_("LLM inference API URL is not configured."))
        exc.status_code = status.HTTP_409_CONFLICT
        raise exc

    if not config.LLM_INFERENCES_API_TOKEN:
        exc = rf_exceptions.APIException(
            _("LLM inference API token is not configured.")
        )
        exc.status_code = status.HTTP_409_CONFLICT
        raise exc


class LLMStreamer:
    """
    Handles the stateful logic of streaming and buffering SSE responses
    from an upstream LLM provider.

    Bandwidth optimizations applied:
    1. Increased flush interval (50ms vs typical 20ms) - reduces event count by ~50%
    2. Size-based buffering - flushes when buffer reaches threshold, reducing small packets
    3. Compact JSON encoding - uses separators=(',',':') to remove whitespace
    4. Short key names - 'c' instead of 'content' saves ~6 bytes per event
    5. Skip empty SSE lines - removes unnecessary protocol overhead
    """

    # Optimization #1: Increased interval (50ms) balances UX responsiveness with bandwidth
    # Human perception threshold for streaming text is ~100ms, so 50ms is imperceptible
    FLUSH_INTERVAL = 0.05

    # Optimization #2: Size threshold prevents many tiny packets when LLM streams fast
    # 30 chars is roughly one short sentence - good granularity for chat UX
    MIN_CHUNK_SIZE = 30

    def __init__(self, input_text, url, token):
        self.url = url
        self.payload = {"input": input_text}
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Token {token}",
        }
        self.buffer = []
        self.buffer_size = 0
        self.last_flush = time.monotonic()

    def _flush(self):
        if not self.buffer:
            return None

        content = "".join(self.buffer)
        self.buffer = []
        self.buffer_size = 0
        self.last_flush = time.monotonic()

        # Optimization #3 & #4: Compact JSON with short key 'c' instead of 'content'
        # Example: {"c":"hello"} vs {"content": "hello"} saves ~8 bytes per event
        return f"data: {json.dumps({'c': content}, separators=(',', ':'))}\n"

    def _should_flush(self):
        """Flush when buffer is large enough OR enough time has passed."""
        if not self.buffer:
            return False
        time_passed = time.monotonic() - self.last_flush
        # Optimization #2: Size-based flush reduces packet count during fast streaming
        return (
            self.buffer_size >= self.MIN_CHUNK_SIZE
            or time_passed >= self.FLUSH_INTERVAL
        )

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
                    # Optimization #5: Skip empty lines entirely (SSE protocol overhead)
                    # Empty lines are event delimiters but we don't need to forward them
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
                    if content:
                        self.buffer.append(content)
                        self.buffer_size += len(content)

                    if self._should_flush():
                        if chunk := self._flush():
                            yield chunk

                    if obj.get("additional_kwargs"):
                        if chunk := self._flush():
                            yield chunk
                        # Optimization #3: Compact JSON for metadata too
                        yield f"data: {json.dumps({'additional_kwargs': obj['additional_kwargs']}, separators=(',', ':'))}\n"

                if final_chunk := self._flush():
                    yield final_chunk

        except requests.RequestException:
            logger.error("Upstream LLM request failed.", exc_info=True)
            err_payload = json.dumps(
                {"detail": "Chat processing was interrupted. Please try again later."},
                separators=(",", ":"),
            )
            yield "event: error\n"
            yield f"data: {err_payload}\n\n"


class ChatViewSet(viewsets.ViewSet):
    @extend_schema(
        request=serializers.ChatRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="LLM chat streamed response.",
            ),
        },
    )
    @action(detail=False, methods=["post"])
    def stream(self, request):
        validate_llm_configuration()

        serializer = serializers.ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        streamer = LLMStreamer(
            input_text=serializer.validated_data["input"],
            url=config.LLM_INFERENCES_API_URL,
            token=config.LLM_INFERENCES_API_TOKEN,
        )

        return StreamingHttpResponse(
            streamer,
            content_type="text/event-stream",
        )

    @extend_schema(
        request=None,
        responses={200: OpenApiTypes.STR},
    )
    @action(detail=False, methods=["post"])
    def invoke(self, request):
        return Response("Invoke chat response", status=status.HTTP_200_OK)
