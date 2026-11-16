"""AI Assistant tool: chart an organization's or a project's monthly costs.

The figures come from the same aggregation as ``/api/invoice-items/costs/``
(``invoices.utils.monthly_costs``), which the project cost chart shows. Items
count in the month they are for: a payment of an earlier month lowers that
month. The organization dashboard groups by invoice month instead, so for an
organization the two can differ in such a month.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.utils import timezone
from django.utils.dateformat import format as format_date

from waldur_core.configuration import config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_projects,
)
from waldur_core.structure.models import Project
from waldur_mastermind.chat.chart_spec import chart, series
from waldur_mastermind.chat.tools.account.helpers import (
    invalid_uuid_message,
    unknown_arguments,
    user_accessible_customers,
    user_accessible_projects,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.output_schemas import (
    BOOLEAN,
    INTEGER,
    NUMBER,
    STRING,
    array,
    obj,
)
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.models import InvoiceItem, PaymentType
from waldur_mastermind.invoices.utils import monthly_costs

_SCOPE = {
    "customer_uuid": STRING,
    "customer_name": STRING,
    "project_uuid": STRING,
    "project_name": STRING,
}

# Either the figures, or only the scope and fixed_price for an organization
# that pays a fixed price.
OUTPUT_SCHEMA = obj(
    {},
    {
        **_SCOPE,
        "fixed_price": BOOLEAN,
        "currency": STRING,
        "months": array(
            obj(
                {
                    "year": INTEGER,
                    "month": INTEGER,
                    "incurred": NUMBER,
                    "compensation": NUMBER,
                    "price": NUMBER,
                }
            )
        ),
        "total_price": NUMBER,
    },
)

DEFAULT_MONTHS = 12
MAX_MONTHS = 24
_CENT = Decimal("0.01")


def _months(count: int) -> list[datetime.date]:
    """The first day of each of the last ``count`` months, oldest first,
    ending with the current month."""
    start = timezone.localdate().replace(day=1)
    return [start - relativedelta(months=n) for n in range(count - 1, -1, -1)]


def _amount(value) -> float:
    return float(Decimal(value or 0).quantize(_CENT))


def _may_see_customer_costs(user, customer) -> bool:
    """As for invoices: owners (LIST_INVOICES on the organization), staff and
    support. Project membership shows only the project, not the organization."""
    return (
        user.is_staff
        or user.is_support
        or has_permission(user, PermissionEnum.LIST_INVOICES, customer)
    )


def _may_see_project_costs(user, project) -> bool:
    """As ``InvoiceItem.Permissions``: the organization's invoice readers, or a
    member of the project when the organization shows billing in projects."""
    if _may_see_customer_costs(user, project.customer):
        return True
    return (
        project.customer.display_billing_info_in_projects
        and Project.objects.filter(
            id=project.id, id__in=get_connected_projects(user)
        ).exists()
    )


def _describe(match) -> str:
    customer = getattr(match, "customer", None)
    where = f"{customer.name}, " if customer else ""
    return f"{match.name} ({where}{match.uuid.hex})"


def _one(queryset, uuid: str, name: str, kind: str):
    """The single match for a UUID or a name, or an error summary."""
    if uuid:
        match = queryset.filter(uuid=uuid).first()
        return (match, "") if match else (None, f"No {kind} with UUID {uuid}.")
    exact = list(queryset.filter(name__iexact=name)[:2])
    matches = exact or list(queryset.filter(name__icontains=name)[:6])
    if not matches:
        return None, f"No {kind} matches '{name}'."
    if len(matches) > 1:
        names = ", ".join(_describe(m) for m in matches[:5])
        return None, f"Several {kind}s match '{name}': {names}. Pass a UUID."
    return matches[0], ""


def cost_chart(title: str, rows: list[dict], currency: str, link: str) -> dict:
    """Stacked bars of incurred cost and compensation per month.

    Compensation is negative, so it stacks below zero; each bar's tooltip
    gives the month's net cost. The current month is an estimate. Without any
    cost the series have no points, so the chart shows its empty message.
    """
    has_costs = any(row["incurred"] or row["compensation"] for row in rows)
    net = [f"Net {row['price']:,.2f} {currency}" for row in rows]

    def points(key):
        return [[i, row[key]] for i, row in enumerate(rows)] if has_costs else []

    items = [
        series(
            "Cost", points("incurred"), tone="info", labels=net if has_costs else None
        )
    ]
    if any(row["compensation"] for row in rows):
        items.append(
            series(
                "Credit and compensation",
                points("compensation"),
                tone="success",
                labels=net,
            )
        )
    total = sum(row["price"] for row in rows)
    return chart(
        "stacked_bar",
        title,
        x={"type": "category", "categories": [row["label"] for row in rows]},
        y={"type": "value", "label": currency},
        series=items,
        subtitle=(
            f"{total:,.2f} {currency} net over {len(rows)} months; "
            "the current month is an estimate"
        ),
        link={"label": "Open costs", "url": link},
        empty_message="No costs have been recorded in this period.",
    )


class ChartCostsTool(BaseTool):
    """Monthly costs of one organization or project, as a chart and figures."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.CHART_COSTS,
            category=ToolCategory.ACCOUNT,
            description=(
                "Chart the monthly costs of one organization or one project: "
                "cost incurred, credit and compensation, and the net cost per "
                "month, ending with the current month (an estimate). Use for "
                "'show/visualize/plot my costs' or 'how did costs develop'. "
                "Pass customer_uuid or customer_name for an organization, or "
                "project_uuid or project_name for a project; with both, the "
                "organization only narrows which project is meant."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Organization (customer) UUID.",
                    },
                    "customer_name": {
                        "type": "string",
                        "description": "Organization name, when no UUID is known.",
                    },
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Project UUID.",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Project name, when no UUID is known.",
                    },
                    "months": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MONTHS,
                        "description": (
                            f"How many months, ending with the current one. "
                            f"Default {DEFAULT_MONTHS}."
                        ),
                    },
                },
                "required": [],
            },
            mcp_exposed=True,
            read_only=True,
            output_schema=OUTPUT_SCHEMA,
            usage_instructions=(
                "The chart is shown to the user; describe the trend in a "
                "sentence or two from the returned figures instead of "
                "repeating every month. 'price' is already net of "
                "compensation: never subtract compensation from it again. "
                "For invoice documents use list_invoices; for credit "
                "balances use the credit tools."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        if error := unknown_arguments(arguments, self.definition):
            return {"type": "validation_error", "summary": error}
        text = {
            key: str(arguments.get(key) or "").strip()
            for key in (
                "customer_uuid",
                "customer_name",
                "project_uuid",
                "project_name",
            )
        }
        for key in ("customer_uuid", "project_uuid"):
            if text[key] and not validate_uuid(text[key]):
                return {
                    "type": "validation_error",
                    "summary": invalid_uuid_message(key, text[key]),
                }
        by_project = bool(text["project_uuid"] or text["project_name"])
        by_customer = bool(text["customer_uuid"] or text["customer_name"])
        if not by_project and not by_customer:
            return {
                "type": "validation_error",
                "summary": "Pass an organization or a project.",
            }
        months = arguments.get("months")
        try:
            months = DEFAULT_MONTHS if months in (None, "") else int(months)
        except (TypeError, ValueError):
            return {"type": "validation_error", "summary": "months must be a number."}
        months = max(1, min(months, MAX_MONTHS))

        if by_project:
            projects = user_accessible_projects(user)
            if by_customer:
                # "Project X of organization Y": the organization narrows the
                # project, it is not charted itself.
                customer, error = _one(
                    user_accessible_customers(user),
                    text["customer_uuid"],
                    text["customer_name"],
                    "organization",
                )
                if error:
                    return {"type": "validation_error", "summary": error}
                projects = projects.filter(customer=customer)
            project, error = _one(
                projects,
                text["project_uuid"],
                text["project_name"],
                "project",
            )
            if error:
                return {"type": "validation_error", "summary": error}
            customer = project.customer
            if not _may_see_project_costs(user, project):
                return {
                    "type": "validation_error",
                    "summary": (
                        f"You can't see the costs of project {project.name}: "
                        "its organization shows them to owners only."
                    ),
                }
            items = InvoiceItem.objects.filter(project_uuid=project.uuid.hex)
            title = f"Monthly costs of {project.name}"
            link = f"/projects/{project.uuid.hex}/"
            scope = {"project_uuid": project.uuid.hex, "project_name": project.name}
        else:
            customer, error = _one(
                user_accessible_customers(user),
                text["customer_uuid"],
                text["customer_name"],
                "organization",
            )
            if error:
                return {"type": "validation_error", "summary": error}
            if not _may_see_customer_costs(user, customer):
                return {
                    "type": "validation_error",
                    "summary": (
                        f"You can't see the costs of organization {customer.name}. "
                        "Ask about one of its projects instead."
                    ),
                }
            items = InvoiceItem.objects.filter(invoice__customer=customer)
            title = f"Monthly costs of {customer.name}"
            link = f"/organizations/{customer.uuid.hex}/dashboard/"
            scope = {"customer_uuid": customer.uuid.hex, "customer_name": customer.name}

        if customer.paymentprofile_set.filter(
            is_active=True, payment_type=PaymentType.FIXED_PRICE
        ).exists():
            return {
                "type": "success",
                "summary": (
                    f"{customer.name} pays a fixed price, so its costs are not "
                    "broken down by month."
                ),
                "data": {**scope, "fixed_price": True},
            }

        first = _months(months)[0]
        by_month = {
            (row["cost_year"], row["cost_month"]): row
            for row in monthly_costs(
                # The same boundary as the REST endpoints.
                filter_queryset_for_user(items, user)
            )
            if (row["cost_year"], row["cost_month"]) >= (first.year, first.month)
        }
        current = timezone.localdate().replace(day=1)
        rows = []
        for month in _months(months):
            row = by_month.get((month.year, month.month), {})
            label = format_date(month, "M Y")
            rows.append(
                {
                    "year": month.year,
                    "month": month.month,
                    "label": f"{label} (estimate)" if month == current else label,
                    "incurred": _amount(row.get("incurred")),
                    "compensation": _amount(row.get("compensation")),
                    "price": _amount(row.get("price")),
                }
            )
        currency = config.CURRENCY_NAME or "EUR"
        total = sum(row["price"] for row in rows)
        return {
            "type": "success",
            "summary": (
                f"{title}: {total:,.2f} {currency} net over the last {months} months."
            ),
            "data": {
                **scope,
                "currency": currency,
                "months": [
                    {
                        key: row[key]
                        for key in (
                            "year",
                            "month",
                            "incurred",
                            "compensation",
                            "price",
                        )
                    }
                    for row in rows
                ],
                "total_price": round(total, 2),
            },
            "ui_component": "chart",
            "ui_data": cost_chart(title, rows, currency, link),
        }


tool_registry.register(ChartCostsTool())
