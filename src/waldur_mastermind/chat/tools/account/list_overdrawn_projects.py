"""AI Assistant tool: list projects whose spend exceeds their credit.

Closes the recurring "which projects have run out of credit?" debugging
question. Inspects every ProjectCredit visible to the caller and returns
those whose lifetime invoice item total exceeds the credit value.
"""

import logging
from collections import defaultdict
from decimal import Decimal

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.models import InvoiceItem, ProjectCredit

logger = logging.getLogger(__name__)


class ListOverdrawnProjectsTool(BaseTool):
    """List projects whose lifetime spend exceeds allocated credit."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_OVERDRAWN_PROJECTS,
            category=ToolCategory.ACCOUNT,
            description=(
                "List projects that have spent more than their allocated "
                "project credit (overdrawn). Optionally restrict to a single "
                "organization via customer_name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": (
                            "Restrict scan to one organization (icontains "
                            "match on Customer.name). Omit for all visible."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for questions like 'which projects have exceeded their "
                "credit?' or 'list overdrawn projects in <org>'. The result "
                "is capped — if `_truncated` is True, ask the user to narrow "
                "by organization."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        customer_name = (arguments.get("customer_name") or "").strip()

        # ProjectCredit is customer-role scoped (Permissions.customer_path =
        # "project__customer"). Scope ProjectCredit directly through
        # filter_queryset_for_user rather than through Customer — Customer's
        # Permissions promote a single project role to whole-customer
        # visibility, which would leak sibling-project credits. Staff/support
        # still see everything.
        credits_qs = filter_queryset_for_user(ProjectCredit.objects.all(), user)
        if customer_name:
            credits_qs = credits_qs.filter(
                project__customer__name__icontains=customer_name
            )
        credits = list(credits_qs.select_related("project", "project__customer"))

        # Sum spend per project in a single bulk query instead of one query
        # per credit (the previous N+1). ``total`` is a Python property, so we
        # group in memory rather than aggregating in SQL.
        project_ids = [credit.project_id for credit in credits]
        spent_by_project: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        items = (
            filter_queryset_for_user(InvoiceItem.objects.all(), user)
            .filter(project_id__in=project_ids)
            .select_related("invoice")
        )
        for item in items:
            spent_by_project[item.project_id] += item.total

        overdrawn: list[dict] = []
        for credit in credits:
            spent = spent_by_project[credit.project_id]
            if spent > credit.value:
                overdrawn.append(
                    {
                        "project_name": credit.project.name,
                        "project_uuid": str(credit.project.uuid),
                        "customer_name": credit.project.customer.name,
                        "credit_value": str(credit.value),
                        "spent_to_date": str(spent),
                        "exceeded_by": str(spent - credit.value),
                    }
                )

        overdrawn.sort(key=lambda row: Decimal(row["exceeded_by"]), reverse=True)

        truncated = len(overdrawn) > MAX_LIST_RESULTS
        rows = overdrawn[:MAX_LIST_RESULTS]

        if not rows:
            scope_hint = f" in {customer_name}" if customer_name else ""
            summary = f"No overdrawn projects found{scope_hint}."
        else:
            names = ", ".join(r["project_name"] for r in rows)
            scope_hint = f" in {customer_name}" if customer_name else ""
            summary = f"{len(overdrawn)} overdrawn project(s){scope_hint}: {names}."

        return {
            "type": "success",
            "data": {
                "overdrawn_projects": rows,
                "_total_count": len(overdrawn),
                "_truncated": truncated,
            },
            "summary": summary,
        }


tool_registry.register(ListOverdrawnProjectsTool())
