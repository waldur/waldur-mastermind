import logging

from django.core.exceptions import PermissionDenied

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.chat.input_guards import (
    DetectionAction,
    SeverityLevel,
    get_detection_service,
)
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_INJECTION_BLOCK_MESSAGE = "Unable to process this request. Please try rephrasing."


class ToolExecutor:
    """Executes tools internally — data never leaves Waldur.

    Responsibilities:
    - Injection detection on tool arguments (fail-closed)
    - Dispatch to registered tools via tool_registry
    - Consistent error handling (permission denied, unexpected exceptions)
    """

    def __init__(self, user):
        self.user = user

    @property
    def _is_anonymous(self) -> bool:
        return self.user is None or getattr(self.user, "is_anonymous", False)

    def _user_log_extra(self) -> dict:
        if self._is_anonymous:
            return {"user_id": None, "username": "anonymous"}
        return {"user_id": self.user.id, "username": self.user.username}

    def execute_tool(self, tool_name: str, arguments: dict) -> dict:
        """Route a tool call to its registered handler."""
        log_extra = self._user_log_extra()
        logger.debug(
            "Tool execution: %s",
            tool_name,
            extra={**log_extra, "tool_name": tool_name, "arguments": arguments},
        )

        injection_block = self._check_injection(tool_name, arguments)
        if injection_block:
            return injection_block

        try:
            tool = tool_registry.get(tool_name)
            if not tool:
                error_msg = f"Unknown tool: {tool_name}"
                return {
                    "type": "error",
                    "error": error_msg,
                    "summary": error_msg,
                    "ui_component": "markdown",
                    "ui_data": {"c": error_msg},
                }

            return tool.execute(self.user, arguments)

        except PermissionDenied:
            logger.warning(
                "Permission denied for tool: %s",
                tool_name,
                extra=log_extra,
            )
            return {
                "type": "error",
                "error": "Permission denied",
                "summary": "You don't have permission to perform this action.",
            }

        except Exception:
            logger.exception(
                "Tool execution failed: %s",
                tool_name,
                extra=log_extra,
            )
            return {
                "type": "error",
                "error": "Internal error",
                "summary": "An error occurred while executing the tool.",
            }

    def _check_injection(self, tool_name: str, arguments: dict) -> dict | None:
        """Check tool arguments for injection and PII. Returns error dict or None."""
        try:
            service = get_detection_service()
            result = service.check_tool_arguments(tool_name, arguments)

            detections = [
                ("Injection", result.injection, EventType.CHAT_INJECTION_DETECTED),
                ("PII", result.pii, EventType.CHAT_PII_DETECTED),
            ]
            for label, detection, event_type in detections:
                if not detection.is_detected:
                    continue
                logger.warning(
                    "%s detected in tool arguments [%s] tool=%s score=%.2f",
                    label,
                    detection.severity.value,
                    tool_name,
                    detection.score,
                )
                if detection.severity >= SeverityLevel.HIGH and not self._is_anonymous:
                    # Audit emit requires a real authenticated user for the
                    # scope hookup; anonymous detections are tracked in the
                    # AnonymousChatBudget strike counter, not the audit log.
                    event_logger.emit(
                        f"{label} detected in tool arguments from {{user_username}}: severity={{severity}}, score={{score}}, tool={{tool_name}}.",
                        event_type=event_type,
                        event_context={
                            "user": self.user,
                            "severity": detection.severity.value,
                            "score": f"{detection.score:.2f}",
                            "tool_name": tool_name,
                        },
                        scopes=[self.user],
                    )
                if detection.action == DetectionAction.BLOCK:
                    return {
                        "type": "error",
                        "error": _INJECTION_BLOCK_MESSAGE,
                        "summary": _INJECTION_BLOCK_MESSAGE,
                    }

        except Exception:
            logger.exception(
                "Injection detection failed in tool executor — failing closed"
            )
            return {
                "type": "error",
                "error": _INJECTION_BLOCK_MESSAGE,
                "summary": _INJECTION_BLOCK_MESSAGE,
            }

        return None
