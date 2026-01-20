import logging

from django.core.exceptions import PermissionDenied

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_LIMIT = 10


class ToolExecutor:
    """Executes tools internally — data never leaves Waldur."""

    def __init__(self, user):
        self.user = user

    def execute_tool(self, tool_name: str, arguments: dict) -> dict:
        """Route tool calls to appropriate handlers."""

        logger.debug(
            f"Tool execution: {tool_name}",
            extra={
                "user_id": self.user.id,
                "username": self.user.username,
                "tool_name": tool_name,
                "arguments": arguments,
            },
        )

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
                f"Permission denied for tool: {tool_name}",
                extra={"user_id": self.user.id},
            )
            return {
                "type": "error",
                "error": "Permission denied",
                "summary": "You don't have permission to perform this action.",
            }

        except Exception:
            logger.exception(
                f"Tool execution failed: {tool_name}",
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
