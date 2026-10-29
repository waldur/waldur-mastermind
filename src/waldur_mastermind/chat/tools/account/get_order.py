"""AI Assistant tool: one marketplace order in detail, with its error and reviewers."""

import logging

from waldur_mastermind.chat.tools.account.helpers import (
    decimal_or_none,
    invalid_uuid_message,
    iso_or_none,
    text_arguments,
    unknown_arguments,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.managers import filter_orders_for_user
from waldur_mastermind.marketplace.models import Order

logger = logging.getLogger(__name__)


def _person(user):
    if user is None:
        return None
    return {"uuid": str(user.uuid), "full_name": user.full_name}


class GetOrderTool(BaseTool):
    """Return one order's state, error, people involved and timestamps."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_ORDER,
            category=ToolCategory.ACCOUNT,
            description=(
                "Details of a single marketplace order: state, error message, "
                "who created and reviewed it, and when."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "order_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Order UUID.",
                    },
                },
                "required": ["order_uuid"],
            },
            usage_instructions=(
                "Use for 'why did my order fail', 'who approved order X', "
                "'is my order still pending'. Find the order UUID with "
                "list_orders first. Quote error_message verbatim when the "
                "state is erred; for rejected orders cite the rejection "
                "comments."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        error = unknown_arguments(arguments, self.definition)
        if error:
            return {"type": "validation_error", "summary": error}
        args, error = text_arguments(arguments, "order_uuid")
        if error:
            return {"type": "validation_error", "summary": error}
        order_uuid = args["order_uuid"]
        if not order_uuid:
            return {"type": "validation_error", "summary": "Pass order_uuid."}
        if not validate_uuid(order_uuid):
            return {
                "type": "validation_error",
                "summary": invalid_uuid_message("order_uuid", order_uuid),
            }

        order = (
            filter_orders_for_user(Order.objects.all(), user)
            .select_related(
                "offering",
                "plan",
                "resource",
                "project",
                "project__customer",
                "created_by",
                "consumer_reviewed_by",
                "provider_reviewed_by",
            )
            .filter(uuid=order_uuid)
            .first()
        )
        if order is None:
            # Hidden and missing look the same, so neither is confirmed.
            return {
                "type": "error",
                "summary": "Order not found or not visible to you.",
            }

        state = dict(OrderStates.CHOICES).get(order.state, str(order.state))
        order_type = dict(OrderTypes.CHOICES).get(order.type, str(order.type))
        data = {
            "order": {
                "uuid": str(order.uuid),
                "type": order_type,
                "state": state,
                "error_message": order.error_message or "",
                "consumer_rejection_comment": order.consumer_rejection_comment,
                "provider_rejection_comment": order.provider_rejection_comment,
                "request_comment": order.request_comment or "",
                "cost": decimal_or_none(order.cost),
                "created": iso_or_none(order.created),
                "modified": iso_or_none(order.modified),
                "consumer_reviewed_at": iso_or_none(order.consumer_reviewed_at),
                "provider_reviewed_at": iso_or_none(order.provider_reviewed_at),
                "completed_at": iso_or_none(order.completed_at),
            },
            "created_by": _person(order.created_by),
            "consumer_reviewed_by": _person(order.consumer_reviewed_by),
            "provider_reviewed_by": _person(order.provider_reviewed_by),
            "offering": {"uuid": str(order.offering.uuid), "name": order.offering.name},
            "plan": (
                {"uuid": str(order.plan.uuid), "name": order.plan.name}
                if order.plan_id
                else None
            ),
            "resource": {"uuid": str(order.resource.uuid), "name": order.resource.name},
            "project": {"uuid": str(order.project.uuid), "name": order.project.name},
            "organization": {
                "uuid": str(order.project.customer.uuid),
                "name": order.project.customer.name,
            },
        }

        summary = (
            f"{order_type} order for '{order.resource.name}' on "
            f"'{order.offering.name}' is {state}."
        )
        if order.error_message:
            summary += f" Error: {order.error_message}"

        return {"type": "success", "data": data, "summary": summary}


tool_registry.register(GetOrderTool())
