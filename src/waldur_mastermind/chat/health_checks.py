import json
import logging

import requests
from constance import config
from health_check.backends import BaseHealthCheckBackend
from health_check.exceptions import ServiceUnavailable

logger = logging.getLogger(__name__)


class LLMConfigurationHealthCheck(BaseHealthCheckBackend):
    """Verify LLM settings are configured in Constance."""

    critical_service = True

    def check_status(self):
        """Check that all required LLM configuration is set."""
        if not config.LLM_CHAT_ENABLED:
            self.add_error(ServiceUnavailable("LLM_CHAT_ENABLED is False"))
            return

        if not config.LLM_INFERENCES_API_URL:
            self.add_error(ServiceUnavailable("LLM_INFERENCES_API_URL not set"))
            return

        if not config.LLM_INFERENCES_API_TOKEN:
            self.add_error(ServiceUnavailable("LLM_INFERENCES_API_TOKEN not set"))

    def identifier(self):
        return "LLM Configuration"


class LLMConnectivityHealthCheck(BaseHealthCheckBackend):
    """Verify LLM API endpoint is reachable."""

    critical_service = True

    def check_status(self):
        """Check connectivity to LLM API endpoint."""
        try:
            url = config.LLM_INFERENCES_API_URL
            if not url:
                self.add_error(
                    ServiceUnavailable("LLM_INFERENCES_API_URL not configured")
                )
                return

            # Simple connectivity check - just try to connect
            response = requests.head(
                url,
                headers={"Authorization": f"Token {config.LLM_INFERENCES_API_TOKEN}"},
                timeout=5,
            )

            # Accept any non-5xx response as connectivity is OK
            if response.status_code >= 500:
                self.add_error(
                    ServiceUnavailable(f"LLM API returned {response.status_code}")
                )

        except requests.RequestException as e:
            self.add_error(ServiceUnavailable(f"Cannot reach LLM API: {e}"))

    def identifier(self):
        return "LLM Connectivity"


class LLMResponseHealthCheck(BaseHealthCheckBackend):
    """Verify LLM can generate a basic response."""

    # Not critical - degraded but API might still work
    critical_service = False

    def check_status(self):
        """Test that LLM can generate a response to a simple prompt."""
        try:
            url = config.LLM_INFERENCES_API_URL
            token = config.LLM_INFERENCES_API_TOKEN

            if not url or not token:
                self.add_error(ServiceUnavailable("LLM not configured"))
                return

            response = requests.post(
                url,
                json={"input": "Say 'OK' if you can read this."},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Token {token}",
                },
                timeout=30,
                stream=True,
            )
            response.raise_for_status()

            # Check we got some content back
            content = ""
            for line in response.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    payload = json.loads(line[6:])
                    content += payload.get("content", "")

            if not content.strip():
                self.add_error(ServiceUnavailable("LLM returned empty response"))
            elif "OK" not in content:
                self.add_error(
                    ServiceUnavailable(
                        f"LLM response did not contain expected 'OK': {content[:100]}"
                    )
                )

        except requests.RequestException as e:
            self.add_error(ServiceUnavailable(f"LLM request failed: {e}"))
        except json.JSONDecodeError as e:
            self.add_error(ServiceUnavailable(f"Invalid JSON response: {e}"))

    def identifier(self):
        return "LLM Response"
