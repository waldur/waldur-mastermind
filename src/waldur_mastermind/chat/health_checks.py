import logging

import openai
from constance import config
from health_check.backends import BaseHealthCheckBackend
from health_check.exceptions import ServiceUnavailable

from waldur_mastermind.chat.providers import (
    ALLOWED_COMPLETION_KEYS,
    FALLBACK_DEFAULTS,
    PROVIDER_DEFAULTS,
)

logger = logging.getLogger(__name__)


class LLMConfigurationHealthCheck(BaseHealthCheckBackend):
    """Verify AI Assistant settings are configured in Constance."""

    critical_service = True

    def check_status(self):
        """Check that all required AI Assistant configurations are set."""
        if not config.AI_ASSISTANT_ENABLED:
            self.add_error(ServiceUnavailable("AI_ASSISTANT_ENABLED is False"))
            return

        if not config.AI_ASSISTANT_API_URL:
            self.add_error(ServiceUnavailable("AI_ASSISTANT_API_URL not set"))
            return

        if not config.AI_ASSISTANT_API_TOKEN:
            self.add_error(ServiceUnavailable("AI_ASSISTANT_API_TOKEN not set"))

    def identifier(self):
        return "AI Assistant Configuration"


class LLMConnectivityHealthCheck(BaseHealthCheckBackend):
    """Verify AI Assistant API endpoint is reachable."""

    critical_service = True

    def check_status(self):
        """Check connectivity to AI Assistant API endpoint."""
        try:
            url = config.AI_ASSISTANT_API_URL
            if not url:
                self.add_error(
                    ServiceUnavailable("AI_ASSISTANT_API_URL not configured")
                )
                return

            # Use the OpenAI SDK to list models — probes connectivity without
            # generating a completion. Works for OpenAI, vLLM, and Ollama.
            # Some providers don't implement /models; treat that as connectivity OK.
            client = openai.OpenAI(
                api_key=config.AI_ASSISTANT_API_TOKEN,
                base_url=url,
                timeout=5.0,
            )
            try:
                client.models.list()
            except openai.APIStatusError as e:
                if e.status_code == 404:
                    # Provider doesn't expose /models — connectivity still confirmed
                    pass
                else:
                    raise

        except openai.APIError as e:
            self.add_error(ServiceUnavailable(f"Cannot reach AI Assistant API: {e}"))

    def identifier(self):
        return "AI Assistant Connectivity"


class LLMResponseHealthCheck(BaseHealthCheckBackend):
    """Verify LLM can generate a basic response."""

    # Not critical - degraded but API might still work
    critical_service = False

    def check_status(self):
        """Test that LLM can generate a response to a simple prompt."""
        try:
            url = config.AI_ASSISTANT_API_URL
            token = config.AI_ASSISTANT_API_TOKEN
            model = config.AI_ASSISTANT_MODEL

            if not url or not token:
                self.add_error(ServiceUnavailable("LLM not configured"))
                return

            # Use streaming (same code path as production) to validate the full
            # request → token → streamed-response flow.
            # Apply AI_ASSISTANT_COMPLETION_KWARGS so the health check tests the same
            # configuration as production (e.g. reasoning_effort, provider defaults).
            backend_type = config.AI_ASSISTANT_BACKEND_TYPE
            completion_kwargs = config.AI_ASSISTANT_COMPLETION_KWARGS
            if not isinstance(completion_kwargs, dict):
                completion_kwargs = {}

            kwargs = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "stream": True,
            }
            for key, value in PROVIDER_DEFAULTS.get(
                backend_type, FALLBACK_DEFAULTS
            ).items():
                kwargs[key] = value
            for key, value in completion_kwargs.items():
                if key in ALLOWED_COMPLETION_KEYS:
                    kwargs[key] = value

            client = openai.OpenAI(api_key=token, base_url=url, timeout=10.0)
            content_parts = []
            with client.chat.completions.create(**kwargs) as stream:
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        content_parts.append(chunk.choices[0].delta.content)

            content = "".join(content_parts)
            if not content.strip():
                self.add_error(ServiceUnavailable("LLM returned empty response"))
            elif "ok" not in content.strip().lower():
                self.add_error(
                    ServiceUnavailable(
                        f"LLM returned unexpected response: {content.strip()!r}"
                    )
                )

        except openai.APIError as e:
            self.add_error(ServiceUnavailable(f"LLM request failed: {e}"))

    def identifier(self):
        return "LLM Response"
