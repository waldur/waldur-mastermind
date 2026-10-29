"""AI Assistant tool: list monthly invoices, filtered by organization and period."""

import logging
from decimal import ROUND_HALF_UP, Decimal

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import (
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
from waldur_mastermind.invoices.models import Invoice

logger = logging.getLogger(__name__)

_STATES = [value for value, _ in Invoice.States.CHOICES]
_CENT = Decimal("0.01")


def _amounts(invoice: Invoice) -> dict:
    """Price, VAT and total as the invoice PDF prints them.

    Invoice.tax and Invoice.total are unrounded; get_invoice_pdf_data rounds
    VAT half-up once and adds it to the price, which is already in cents.
    """
    price = invoice.price
    tax = (price * invoice.tax_percent / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    return {"price": str(price), "tax": str(tax), "total": str(price + tax)}


def _as_int(value) -> int | None:
    """``value`` as an int; digit strings count, booleans do not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # isdigit() also accepts superscripts such as "²", which int() rejects.
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value)
    return None


class ListInvoicesTool(BaseTool):
    """Return the invoices the user can see, newest period first."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_INVOICES,
            category=ToolCategory.ACCOUNT,
            description=(
                "List monthly invoices with state and totals, optionally "
                "filtered by organization, year, month or state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Only invoices of this organization.",
                    },
                    "customer_name": {
                        "type": "string",
                        "description": (
                            "Organization name (icontains) when no UUID is known."
                        ),
                    },
                    "year": {
                        "type": "integer",
                        "description": "Only invoices for this year.",
                    },
                    "month": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 12,
                        "description": "Only invoices for this month (1-12).",
                    },
                    "state": {
                        "type": "array",
                        "items": {"type": "string", "enum": _STATES},
                        "description": (
                            "Only invoices in these states. 'created' means "
                            "issued and unpaid."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for 'what did we pay last month', 'show our invoices for "
                "2026', 'is the March invoice paid'. Invoices belong to "
                "organizations; pass customer_name when the user names one. "
                "States: 'pending' is the current month, still growing; "
                "'pending_finalization' is a closed month not yet issued; "
                "'created' is issued and awaiting payment, so unpaid; 'paid' "
                "is settled; 'canceled' is void. For 'what is unpaid' filter "
                "on created. `tax` and `total` are rounded to the cent as on "
                "the invoice PDF, so they can differ by a cent from "
                "/api/invoices/. For credits and "
                "compensations use explain_invoice_compensations. "
                "A zero result only means nothing is visible to this user: say "
                "so, and never invent a reason why the list is empty."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        error = unknown_arguments(arguments, self.definition)
        if error:
            return {"type": "validation_error", "summary": error}
        args, error = text_arguments(arguments, "customer_uuid", "customer_name")
        if error:
            return {"type": "validation_error", "summary": error}
        customer_uuid = args["customer_uuid"]
        customer_name = args["customer_name"]
        year = arguments.get("year")
        month = arguments.get("month")
        states = string_list(arguments.get("state"))

        if customer_uuid and not validate_uuid(customer_uuid):
            return {
                "type": "validation_error",
                "summary": invalid_uuid_message("customer_uuid", customer_uuid),
            }
        parsed = {}
        for arg_name, value, low, high in [
            ("year", year, 1970, 9999),
            ("month", month, 1, 12),
        ]:
            if value is None:
                parsed[arg_name] = None
                continue
            number = _as_int(value)
            if number is None or not low <= number <= high:
                return {
                    "type": "validation_error",
                    "summary": f"Invalid {arg_name}: {value}",
                }
            parsed[arg_name] = number
        year, month = parsed["year"], parsed["month"]
        if states is None:
            return {
                "type": "validation_error",
                "summary": "state must be a list of strings.",
            }
        unknown = [s for s in states if s not in _STATES]
        if unknown:
            return {
                "type": "validation_error",
                "summary": (
                    f"Unknown invoice state: {', '.join(unknown)}. Valid "
                    f"states: {', '.join(_STATES)}."
                ),
            }

        qs = filter_queryset_for_user(Invoice.objects.all(), user)
        if customer_uuid:
            qs = qs.filter(customer__uuid=customer_uuid)
        if customer_name:
            qs = qs.filter(customer__name__icontains=customer_name)
        if year is not None:
            qs = qs.filter(year=year)
        if month is not None:
            qs = qs.filter(month=month)
        if states:
            qs = qs.filter(state__in=states)

        qs = qs.select_related("customer").order_by("-year", "-month", "id")

        total = qs.count()
        truncated = total > MAX_LIST_RESULTS
        # Invoice.price is summed in Python from the items.
        rows = qs.prefetch_related("items")[:MAX_LIST_RESULTS]
        invoices = [
            {
                "uuid": str(inv.uuid),
                "number": inv.number,
                "organization_name": inv.customer.name,
                "organization_uuid": str(inv.customer.uuid),
                "year": inv.year,
                "month": inv.month,
                "state": inv.state,
                **_amounts(inv),
                "invoice_date": iso_or_none(inv.invoice_date),
                "due_date": iso_or_none(inv.due_date),
            }
            for inv in rows
        ]

        summary = f"Found {total} invoice{'s' if total != 1 else ''}"
        if truncated:
            summary += (
                f" (showing latest {MAX_LIST_RESULTS} — narrow by year or "
                "organization to see more)"
            )
        summary += "."
        # Same note whether or not hidden invoices exist, so it reveals nothing.
        if total == 0 and not (user.is_staff or user.is_support):
            summary += (
                " This only covers invoices your role lets you see; there may be "
                "others you cannot see."
            )

        return {
            "type": "success",
            "data": {
                "invoices": invoices,
                "total": total,
                "_total_count": total,
                "_truncated": truncated,
            },
            "summary": summary,
        }


tool_registry.register(ListInvoicesTool())
