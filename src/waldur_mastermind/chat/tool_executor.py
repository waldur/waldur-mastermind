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

            return {
                "type": "error",
                "error": f"Unknown tool: {tool_name}",
                "summary": f"Unknown tool: {tool_name}",
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

        NB! This returns only the number of total resources and a breakdown by type, it does not yet create a table view.
        """

        queryset = (
            filter_queryset_for_user(
                Resource.objects.exclude(state=ResourceStates.TERMINATED),
                self.user,
            )
            .select_related("project", "project__customer", "offering")
            .only(
                "uuid",
                "name",
                "state",
                "created",
                "project__uuid",
                "project__name",
                "project__customer__name",
                "offering__type",
            )[:DEFAULT_RESOURCE_LIMIT]
        )

        # Build resource list and count types
        resources = []
        type_counts = {}

        for r in queryset:
            offering_type = r.offering.type
            type_counts[offering_type] = type_counts.get(offering_type, 0) + 1

            resources.append(
                {
                    "uuid": str(r.uuid),
                    "name": r.name,
                    "type": offering_type,
                    "state": r.get_state_display(),
                    "project": r.project.name if r.project else None,
                    "project_uuid": str(r.project.uuid) if r.project else None,
                    "customer": (
                        r.project.customer.name
                        if r.project and r.project.customer
                        else None
                    ),
                    "created": r.created.isoformat() if r.created else None,
                }
            )

        total = len(resources)

        # Generate summary for LLM (no sensitive data)
        if total == 0:
            summary = "No resources found."
        else:
            # Create Markdown table
            header = (
                "| Name | Type | State | Project | Customer |\n|---|---|---|---|---|\n"
            )
            rows = (
                "\n".join(
                    f"| {res['name']} | {res['type']} | {res['state']} | {res['project']} | {res['customer']} |"
                    for res in resources
                )
                + "\n"
            )

            intro = f"Found {total} resource{'s' if total != 1 else ''}: \n\n"
            summary = intro + header + rows

        logger.debug(
            "Resource query successful",
            extra={
                "user_id": self.user.id,
                "total_count": total,
                "type_counts": type_counts,
            },
        )

        return {
            "type": "success",
            "data": {
                "resources": resources,
                "total": total,
                "type_counts": type_counts,
            },
            "summary": summary,
        }
