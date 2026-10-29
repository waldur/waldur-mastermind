"""AI Assistant tool: list marketplace orders, filtered by project, resource or state."""

import logging

from waldur_mastermind.chat.tools.account.helpers import (
    decimal_or_none,
    invalid_uuid_message,
    iso_or_none,
    string_list,
    text_arguments,
    unknown_arguments,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.managers import filter_orders_for_user
from waldur_mastermind.marketplace.models import Order

logger = logging.getLogger(__name__)

_STATE_LABELS = dict(OrderStates.CHOICES)
_STATE_BY_LABEL = {label: value for value, label in OrderStates.CHOICES}
# "pending" is what people ask for; it covers every state waiting on someone.
_PENDING_LABELS = [label for label in _STATE_BY_LABEL if label.startswith("pending-")]
_TYPE_LABELS = dict(OrderTypes.CHOICES)


class ListOrdersTool(BaseTool):
    """Return the orders the user can see, newest first."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_ORDERS,
            category=ToolCategory.ACCOUNT,
            description=(
                "List marketplace orders (create, update, terminate "
                "requests), optionally filtered by organization, project, "
                "resource, offering or state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Only orders in this project.",
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Project name (icontains) when no UUID is known."
                        ),
                    },
                    "resource_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Only orders for this resource.",
                    },
                    "customer_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Only orders placed by this organization.",
                    },
                    "customer_name": {
                        "type": "string",
                        "description": (
                            "Ordering organization name (icontains) when no "
                            "UUID is known."
                        ),
                    },
                    "offering_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Only orders for this offering.",
                    },
                    "offering_name": {
                        "type": "string",
                        "description": (
                            "Offering name (icontains) when no UUID is known."
                        ),
                    },
                    "state": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [*_STATE_BY_LABEL, "pending"],
                        },
                        "description": (
                            'Only orders in these states. "pending" means '
                            "every pending-* state."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for 'what orders do I have', 'which orders are pending', "
                "'did my order for X go through'. Filter by project_name when "
                "the user names a project, by customer_name for the ordering "
                "organization, and by offering_name for a service "
                "provider asking about orders for their offering ('which orders "
                "came in for X'). Resolve a resource UUID first with "
                "get_project_resources. "
                "For one order's error and reviewers use get_order. "
                "A zero result only means nothing is visible to this user: say "
                "so, and never invent a reason why the list is empty."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        error = unknown_arguments(arguments, self.definition)
        if error:
            return {"type": "validation_error", "summary": error}
        args, error = text_arguments(
            arguments,
            "project_uuid",
            "project_name",
            "resource_uuid",
            "offering_uuid",
            "offering_name",
            "customer_uuid",
            "customer_name",
        )
        if error:
            return {"type": "validation_error", "summary": error}
        project_uuid = args["project_uuid"]
        project_name = args["project_name"]
        resource_uuid = args["resource_uuid"]
        offering_uuid = args["offering_uuid"]
        offering_name = args["offering_name"]
        customer_uuid = args["customer_uuid"]
        customer_name = args["customer_name"]
        states = string_list(arguments.get("state"))

        for arg_name, value in [
            ("project_uuid", project_uuid),
            ("resource_uuid", resource_uuid),
            ("offering_uuid", offering_uuid),
            ("customer_uuid", customer_uuid),
        ]:
            if value and not validate_uuid(value):
                return {
                    "type": "validation_error",
                    "summary": invalid_uuid_message(arg_name, value),
                }
        if states is None:
            return {
                "type": "validation_error",
                "summary": "state must be a list of strings.",
            }
        states = [
            label
            for s in states
            for label in (_PENDING_LABELS if s == "pending" else [s])
        ]
        unknown = [s for s in states if s not in _STATE_BY_LABEL]
        if unknown:
            return {
                "type": "validation_error",
                "summary": (
                    f"Unknown order state: {', '.join(unknown)}. Valid states: "
                    f"{', '.join(_STATE_BY_LABEL)}."
                ),
            }

        qs = filter_orders_for_user(Order.objects.all(), user)
        if project_uuid:
            qs = qs.filter(project__uuid=project_uuid)
        if project_name:
            qs = qs.filter(project__name__icontains=project_name)
        if customer_uuid:
            qs = qs.filter(project__customer__uuid=customer_uuid)
        if customer_name:
            qs = qs.filter(project__customer__name__icontains=customer_name)
        if offering_uuid:
            qs = qs.filter(offering__uuid=offering_uuid)
        if offering_name:
            qs = qs.filter(offering__name__icontains=offering_name)
        if resource_uuid:
            qs = qs.filter(resource__uuid=resource_uuid)
        if states:
            qs = qs.filter(state__in=[_STATE_BY_LABEL[s] for s in states])

        qs = qs.select_related(
            "offering", "resource", "project", "project__customer", "created_by"
        ).order_by("-created", "id")

        total = qs.count()
        truncated = total > MAX_LIST_RESULTS
        orders = [
            {
                "uuid": str(o.uuid),
                "type": _TYPE_LABELS.get(o.type, str(o.type)),
                "state": _STATE_LABELS.get(o.state, str(o.state)),
                "offering_name": o.offering.name,
                "resource_name": o.resource.name,
                "resource_uuid": str(o.resource.uuid),
                "project_name": o.project.name,
                "project_uuid": str(o.project.uuid),
                "organization_name": o.project.customer.name,
                "organization_uuid": str(o.project.customer.uuid),
                "cost": decimal_or_none(o.cost),
                "created_by": o.created_by.full_name if o.created_by_id else "",
                "created": iso_or_none(o.created),
                "error_message": o.error_message or "",
                "consumer_rejection_comment": o.consumer_rejection_comment,
                "provider_rejection_comment": o.provider_rejection_comment,
            }
            for o in qs[:MAX_LIST_RESULTS]
        ]

        summary = f"Found {total} order{'s' if total != 1 else ''}"
        if truncated:
            summary += (
                f" (showing first {MAX_LIST_RESULTS} — narrow filters to see more)"
            )
        summary += "."
        # Same note whether or not hidden orders exist, so it reveals nothing.
        if total == 0 and not (user.is_staff or user.is_support):
            summary += (
                " This only covers orders your role lets you see; there may be "
                "others you cannot see."
            )

        return {
            "type": "success",
            "data": {
                "orders": orders,
                "total": total,
                "_total_count": total,
                "_truncated": truncated,
            },
            "summary": summary,
        }


tool_registry.register(ListOrdersTool())
