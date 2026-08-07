"""AI Assistant tool: project credit balance with current-month spend.

Closes the recurring "why did project X's credit go to zero?" debugging
question that operators currently work out by hand from CustomerCredit /
ProjectCredit / InvoiceItem joins.
"""

import logging
from decimal import Decimal

from django.utils import timezone

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import (
    sum_invoice_item_totals,
    user_accessible_projects,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.models import (
    CustomerCredit,
    InvoiceItem,
    ProjectCredit,
)

logger = logging.getLogger(__name__)


class ExplainProjectCreditBalanceTool(BaseTool):
    """Show a project's credit allocation, current-month spend, and balance."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.EXPLAIN_PROJECT_CREDIT_BALANCE,
            category=ToolCategory.ACCOUNT,
            description=(
                "Explain a project's credit balance: how much credit was "
                "allocated, how much has been spent this month and to date, "
                "and whether the project is overdrawn. Also surfaces the "
                "parent organization's customer credit envelope for context. "
                "Accepts project_uuid OR project_name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Project UUID (exact match).",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Project name (icontains) when no UUID is known.",
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for questions like 'what's the credit balance for "
                "project X?', 'why did project X run out of credit?', or "
                "'how much has project X spent this month?'. Prefer "
                "project_uuid when available."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        project_uuid = (arguments.get("project_uuid") or "").strip()
        project_name = (arguments.get("project_name") or "").strip()

        if not project_uuid and not project_name:
            return {
                "type": "validation_error",
                "summary": "Pass project_uuid or project_name.",
            }
        if project_uuid and not validate_uuid(project_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for project_uuid: {project_uuid}",
            }

        qs = user_accessible_projects(user)
        if project_uuid:
            project = qs.filter(uuid=project_uuid).first()
        else:
            project = qs.filter(name__icontains=project_name).first()
        if project is None:
            return {"type": "error", "summary": "Project not found."}

        # Scope through filter_queryset_for_user to match the REST boundary:
        # ProjectCredit and InvoiceItem are readable by project roles for their
        # own project (invoice items only when the customer opts in via
        # display_billing_info_in_projects), while CustomerCredit stays
        # customer-role scoped, so a project-only member never sees the
        # organization envelope. Staff/support still see everything.
        project_credit = (
            filter_queryset_for_user(ProjectCredit.objects.all(), user)
            .filter(project=project)
            .first()
        )
        customer_credit = (
            filter_queryset_for_user(CustomerCredit.objects.all(), user)
            .filter(customer=project.customer)
            .first()
        )

        now = timezone.now()
        accessible_items = filter_queryset_for_user(InvoiceItem.objects.all(), user)
        items_this_month = accessible_items.filter(
            project=project,
            invoice__year=now.year,
            invoice__month=now.month,
        )
        items_to_date = accessible_items.filter(project=project)

        spent_this_month = sum_invoice_item_totals(items_this_month)
        spent_to_date = sum_invoice_item_totals(items_to_date)

        project_credit_data: dict | None = None
        is_overdrawn = False
        balance_remaining: Decimal | None = None
        if project_credit:
            balance_remaining = project_credit.value - spent_to_date
            is_overdrawn = balance_remaining < 0
            project_credit_data = {
                "value": str(project_credit.value),
                "end_date": project_credit.end_date.isoformat()
                if project_credit.end_date
                else None,
                "spent_to_date": str(spent_to_date),
                "spent_this_month": str(spent_this_month),
                "balance_remaining": str(balance_remaining),
                "is_overdrawn": is_overdrawn,
            }

        customer_credit_data: dict | None = None
        if customer_credit:
            customer_credit_data = {
                "value": str(customer_credit.value),
                "expected_consumption": str(customer_credit.expected_consumption),
                "end_date": customer_credit.end_date.isoformat()
                if customer_credit.end_date
                else None,
                "allocated_to_projects": str(customer_credit.allocated_to_projects),
            }

        if project_credit:
            if is_overdrawn:
                summary = (
                    f"{project.name} is OVERDRAWN: spent {spent_to_date} of "
                    f"{project_credit.value} project credit "
                    f"(over by {-balance_remaining})."
                )
            else:
                summary = (
                    f"{project.name} has {balance_remaining} of "
                    f"{project_credit.value} project credit remaining; "
                    f"spent {spent_this_month} this month."
                )
        else:
            summary = (
                f"{project.name} has no project credit configured; "
                f"spent {spent_this_month} this month."
            )

        return {
            "type": "success",
            "data": {
                "project": {"uuid": str(project.uuid), "name": project.name},
                "customer": {
                    "uuid": str(project.customer.uuid),
                    "name": project.customer.name,
                },
                "project_credit": project_credit_data,
                "customer_credit": customer_credit_data,
            },
            "summary": summary,
        }


tool_registry.register(ExplainProjectCreditBalanceTool())
