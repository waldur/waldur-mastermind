"""AI Assistant tool: one resource in detail, with its last error and events."""

import logging

from waldur_mastermind.chat.tools.account.helpers import (
    invalid_uuid_message,
    iso_or_none,
    scope_events,
    text_arguments,
    unknown_arguments,
    user_consumer_resources,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.utils import (
    RESOURCE_PROJECT_ONLY_HIDDEN_FIELDS,
    is_resource_project_only_viewer,
)

logger = logging.getLogger(__name__)

RECENT_EVENTS_LIMIT = 10


class GetResourceTool(BaseTool):
    """Return one resource's details, offering, plan, last error and events."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_RESOURCE,
            category=ToolCategory.ACCOUNT,
            description=(
                "Details of a single resource: state, offering, plan, limits "
                "(CPU, RAM, storage quotas), project, last error message and "
                "recent events."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Resource UUID (exact match).",
                    },
                    "resource_name": {
                        "type": "string",
                        "description": (
                            "Resource name (icontains) when no UUID is known. "
                            "Skips terminated resources."
                        ),
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Optional project name to narrow a resource_name search."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for questions about one resource: 'what's wrong with X', "
                "'why did X fail', 'what plan is X on', 'what are the limits "
                "of X', 'what happened to X recently'. Cite error_message when "
                "the state is Erred. "
                "Fields named in `restricted_fields` are null because they are "
                "hidden from this user: say they are not visible to them, never "
                "that they are empty, that no error was recorded, or that they "
                "hold a value. "
                "When several resources match a name, the result lists them "
                "under `matches`; ask the user which one, or call again "
                "with its resource_uuid. For paused resources use "
                "explain_resource_paused_reason instead."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        error = unknown_arguments(arguments, self.definition)
        if error:
            return {"type": "validation_error", "summary": error}
        args, error = text_arguments(
            arguments, "resource_uuid", "resource_name", "project_name"
        )
        if error:
            return {"type": "validation_error", "summary": error}
        resource_uuid = args["resource_uuid"]
        resource_name = args["resource_name"]
        project_name = args["project_name"]

        if not resource_uuid and not resource_name:
            return {
                "type": "validation_error",
                "summary": "Pass resource_uuid or resource_name.",
            }
        if resource_uuid and not validate_uuid(resource_uuid):
            return {
                "type": "validation_error",
                "summary": invalid_uuid_message("resource_uuid", resource_uuid),
            }

        qs = user_consumer_resources(user).select_related(
            "offering",
            "offering__category",
            "plan",
            "project",
            "project__customer",
        )
        state_labels = dict(ResourceStates.CHOICES)

        if resource_uuid:
            qs = qs.filter(uuid=resource_uuid)
        else:
            # Names get reused, so leave terminated resources to UUID lookups.
            qs = qs.filter(name__icontains=resource_name).exclude(
                state=ResourceStates.TERMINATED
            )
            if project_name:
                qs = qs.filter(project__name__icontains=project_name)

        total = qs.count()
        if total == 0:
            # Hidden and missing look the same, so neither is confirmed.
            return {
                "type": "error",
                "summary": "Resource not found or not visible to you.",
            }
        if total > 1 and resource_name:
            # "web-1" also matches "web-1-old"; one exact name is the answer.
            exact = qs.filter(name__iexact=resource_name)
            if exact.count() == 1:
                qs, total = exact, 1

        # Same keys whether one resource or several matched.
        data = {
            "resource": None,
            "offering": None,
            "plan": None,
            "project": None,
            "organization": None,
            "recent_events": [],
            "matches": [],
            "_total_count": total,
            "_truncated": total > MAX_LIST_RESULTS,
        }

        if total > 1:
            rows = qs.order_by("-created")[:MAX_LIST_RESULTS]
            data["matches"] = [
                {
                    "uuid": str(r.uuid),
                    "name": r.name,
                    "state": state_labels.get(r.state, str(r.state)),
                    "project_name": r.project.name,
                    "project_uuid": str(r.project.uuid),
                }
                for r in rows
            ]
            summary = f"{total} resources match '{resource_name}'. Ask which one"
            summary += "." if project_name else ", or narrow by project_name."
            return {"type": "success", "data": data, "summary": summary}

        resource = qs.get()
        offering = resource.offering
        project = resource.project
        state = state_labels.get(resource.state, str(resource.state))
        events = scope_events(user, resource)
        resource_data = {
            "uuid": str(resource.uuid),
            "name": resource.name,
            "state": state,
            "backend_id": resource.backend_id or "",
            "error_message": resource.error_message or "",
            "paused": resource.paused,
            "limits": resource.limits or {},
            "created": iso_or_none(resource.created),
            "modified": iso_or_none(resource.modified),
            "end_date": iso_or_none(resource.end_date),
        }
        restricted_fields = []
        # REST withholds these from users who see the resource only through
        # a ResourceProject role.
        if is_resource_project_only_viewer(user, resource):
            restricted_fields = [
                field
                for field in resource_data
                if field in RESOURCE_PROJECT_ONLY_HIDDEN_FIELDS
            ]
            for field in restricted_fields:
                resource_data[field] = None
        if events is None:
            restricted_fields.append("recent_events")
        resource_data["restricted_fields"] = restricted_fields
        error_message = resource_data["error_message"]

        data |= {
            "resource": resource_data,
            "offering": {
                "uuid": str(offering.uuid),
                "name": offering.name,
                "type": offering.type,
                "category_title": (
                    offering.category.title if offering.category_id else ""
                ),
            },
            "plan": (
                {"uuid": str(resource.plan.uuid), "name": resource.plan.name}
                if resource.plan_id
                else None
            ),
            "project": {"uuid": str(project.uuid), "name": project.name},
            "organization": {
                "uuid": str(project.customer.uuid),
                "name": project.customer.name,
            },
            "recent_events": None
            if events is None
            else [
                {
                    "created": iso_or_none(e.created),
                    "event_type": e.event_type,
                    "message": e.message,
                }
                for e in events[:RECENT_EVENTS_LIMIT]
            ],
        }

        summary = f"{resource.name} is {state}, on offering '{offering.name}'"
        if resource.plan_id:
            summary += f", plan '{resource.plan.name}'"
        summary += "."
        if error_message:
            summary += f" Last error: {error_message}"
        if restricted_fields:
            hidden = ", ".join(f.replace("_", " ") for f in restricted_fields)
            summary += (
                f" Hidden from this user (they may or may not have a value): {hidden}."
            )

        return {"type": "success", "data": data, "summary": summary}


tool_registry.register(GetResourceTool())
