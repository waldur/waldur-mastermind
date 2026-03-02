import logging

from django.core.exceptions import PermissionDenied

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.chat.injection_detection import (
    DetectionAction,
    SeverityLevel,
    get_injection_service,
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

    def execute_tool(self, tool_name: str, arguments: dict) -> dict:
        """Route a tool call to its registered handler."""
        logger.debug(
            "Tool execution: %s",
            tool_name,
            extra={
                "user_id": self.user.id,
                "username": self.user.username,
                "tool_name": tool_name,
                "arguments": arguments,
            },
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
                extra={"user_id": self.user.id},
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
                extra={"user_id": self.user.id},
            )
            return {
                "type": "error",
                "error": "Internal error",
                "summary": "An error occurred while executing the tool.",
            }

    def _check_injection(self, tool_name: str, arguments: dict) -> dict | None:
        """Check tool arguments for prompt injection. Returns error dict or None."""
        try:
            service = get_injection_service()
            result = service.check_tool_arguments(tool_name, arguments)

            if result.is_injection:
                logger.warning(
                    "Injection detected in tool arguments [%s] tool=%s score=%.2f",
                    result.severity.value,
                    tool_name,
                    result.score,
                )
                if result.severity in (SeverityLevel.HIGH, SeverityLevel.CRITICAL):
                    event_logger.emit(
                        "Prompt injection detected in tool arguments from "
                        "{user_username}: severity={severity}, score={score}, tool={tool_name}.",
                        event_type=EventType.CHAT_INJECTION_DETECTED,
                        event_context={
                            "user": self.user,
                            "severity": result.severity.value,
                            "score": f"{result.score:.2f}",
                            "tool_name": tool_name,
                        },
                        scopes=[self.user],
                    )
                if result.action == DetectionAction.BLOCK:
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
