import logging

from django.core.exceptions import PermissionDenied

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.injection_detection import (
    DetectionAction,
    SeverityLevel,
    get_injection_service,
)
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_LIMIT = 10
_INJECTION_BLOCK_MESSAGE = "Unable to process this request. Please try rephrasing."


class ToolExecutor:
    """Executes tools internally — data never leaves Waldur."""

    def __init__(self, user):
        self.user = user

    def execute_tool(self, tool_name: str, arguments: dict) -> dict:
        """Route tool calls to appropriate handlers."""

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

        # Check tool arguments for injection (fail-closed: block on any error)
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
                if result.severity in (
                    SeverityLevel.HIGH,
                    SeverityLevel.CRITICAL,
                ):
                    event_logger.emit(
                        "Prompt injection detected in tool arguments from {user_username}: severity={severity}, score={score}, tool={tool_name}.",
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

        try:
            if tool_name == "show_user_resources":
                return self._show_user_resources()

            error_msg = f"Unknown tool: {tool_name}"
            return {
                "type": "error",
                "error": error_msg,
                "summary": error_msg,
                "ui_component": "markdown",
                "ui_data": {"c": error_msg},
            }

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

    def _show_user_resources(self) -> dict:
        """
        List all resources accessible by the current user.

        Returns structured table data for interactive UI rendering.
        """

        queryset = (
            filter_queryset_for_user(
                Resource.objects.exclude(state=ResourceStates.TERMINATED),
                self.user,
            )
            .select_related(
                "project", "project__customer", "offering", "offering__category"
            )
            .only(
                "uuid",
                "name",
                "state",
                "created",
                "project__uuid",
                "project__name",
                "project__customer__name",
                "offering__name",
                "offering__type",
                "offering__category__title",
            )[:DEFAULT_RESOURCE_LIMIT]
        )

        # Build resource list and count types
        resources = []

        for r in queryset:
            resources.append(
                {
                    "uuid": str(r.uuid),
                    "name": r.name,
                    "category": r.offering.category.title
                    if r.offering.category
                    else "",
                    "offering": r.offering.name,
                    "organization": (
                        r.project.customer.name
                        if r.project and r.project.customer
                        else ""
                    ),
                    "project": r.project.name if r.project else "",
                    "project_uuid": str(r.project.uuid) if r.project else None,
                    "state": r.get_state_display(),
                }
            )

        total = len(resources)

        # Generate summary for LLM (no sensitive data)
        summary = f"Found {total} resource{'s' if total != 1 else ''}"

        # Build structured table data matching frontend format
        headers = ["Name", "Category", "Offering", "Organization", "Project", "State"]
        rows = [
            [
                res["name"],
                res["category"],
                res["offering"],
                res["organization"],
                res["project"],
                res["state"],
            ]
            for res in resources
        ]

        logger.debug(
            "Resource query successful",
            extra={
                "user_id": self.user.id,
                "total_count": total,
            },
        )

        return {
            "type": "success",
            "data": {
                "resources": resources,
                "total": total,
            },
            "summary": summary,
            "ui_component": "table",
            "ui_data": {
                "h": headers,
                "r": rows,
                "n": total,
            },
        }
