import json
import logging

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
        input_text = serializer.validated_data["input"]

        llm_url = config.LLM_INFERENCES_API_URL

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Token {config.LLM_INFERENCES_API_TOKEN}",
        }

        def sse_stream():
            try:
                with requests.post(
                    llm_url,
                    json={"input": input_text},
                    headers=headers,
                    stream=True,
                    timeout=(5, 60),
                ) as response:
                    response.raise_for_status()
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if raw_line is None:
                            continue

                        # Empty line indicates end of SSE event
                        if raw_line == "":
                            yield "\n"
                            continue

                        line = raw_line.strip()

                        if line.startswith("data: "):
                            payload_str = line[len("data: ") :]
                            try:
                                obj = json.loads(payload_str)
                            except json.JSONDecodeError:
                                logger.error(
                                    "Failed to decode LLM SSE payload.", exc_info=True
                                )
                                continue
                            slim = {
                                "content": obj.get("content"),
                                "additional_kwargs": obj.get("additional_kwargs") or {},
                            }
                            yield (
                                "data: " + json.dumps(slim, ensure_ascii=False) + "\n"
                            )
                        else:
                            logger.debug("Dropping upstream SSE line: %s", raw_line)
                            continue
            except requests.RequestException:
                logger.error("Upstream LLM request failed.", exc_info=True)
                payload = json.dumps(
                    {
                        "detail": (
                            "Chat processing was interrupted due to a technical problem. Please try again later."
                        )
                    },
                    ensure_ascii=False,
                )
                yield "event: error\n"
                yield f"data: {payload}\n\n"

        response = StreamingHttpResponse(
            sse_stream(),
            content_type="text/event-stream",
        )

        return response

    @extend_schema(
        request=None,
        responses={200: OpenApiTypes.STR},
    )
    @action(detail=False, methods=["post"])
    def invoke(self, request):
        return Response("Invoke chat response", status=status.HTTP_200_OK)
