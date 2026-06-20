"""AI Assistant tool: whole-organization credit overview.

Answers org-level credit questions ("does organization X have credit?",
"summarize org X's credit health") in a SINGLE call. Without it the
assistant fans out one ``explain_project_credit_balance`` per project to
build a customer summary — slow and noisy. Returns the customer credit
envelope plus a per-project rollup (credit, spend, balance, overdrawn flag).

All querysets are scoped through ``filter_queryset_for_user`` so the
customer/project credit visibility matches the REST boundary (customer-role
scoped; staff/support see everything).
"""

import logging
from collections import defaultdict
from decimal import Decimal

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import (
    user_accessible_customers,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.models import (
    CustomerCredit,
    InvoiceItem,
    ProjectCredit,
)

logger = logging.getLogger(__name__)


class GetCustomerCreditOverviewTool(BaseTool):
    """One-call credit health overview for a whole organization."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_CUSTOMER_CREDIT_OVERVIEW,
            category=ToolCategory.ACCOUNT,
            description=(
                "Whole-organization credit overview in one call: the customer "
                "credit envelope (allocated value, expected consumption, "
                "amount allocated to projects) plus a per-project rollup with "
                "each project's credit, spend, balance and overdrawn flag. Use "
                "for 'does organization X have credit?', 'summarize org X's "
                "credit health', or any question spanning a whole "
                "organization's projects. customer_name must be an "
                "ORGANIZATION/customer name, NOT a single project — for one "
                "project use explain_project_credit_balance. Accepts "
                "customer_uuid OR customer_name."
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
                        "description": (
                            "Organization name (icontains) when no UUID is known."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Prefer this over repeated explain_project_credit_balance "
                "calls when the user asks about an ORGANIZATION's overall "
                "credit (envelope or across-projects health). For a single "
                "project use explain_project_credit_balance; for only the "
                "overdrawn projects use list_overdrawn_projects."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        customer_uuid = (arguments.get("customer_uuid") or "").strip()
        customer_name = (arguments.get("customer_name") or "").strip()

        if not customer_uuid and not customer_name:
            return {
                "type": "validation_error",
                "summary": "Pass customer_uuid or customer_name.",
            }
        if customer_uuid and not validate_uuid(customer_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for customer_uuid: {customer_uuid}",
            }

        customers = user_accessible_customers(user)
        if customer_uuid:
            customer = customers.filter(uuid=customer_uuid).first()
        else:
            customer = customers.filter(name__icontains=customer_name).first()
        if customer is None:
            # The name may be a PROJECT, not an organization — names collide
            # across the two (e.g. "Bluewave Vela" is a project). Point the
            # caller at the project-scoped tool so it can recover.
            ref = customer_name or customer_uuid
            return {
                "type": "error",
                "summary": (
                    f"No organization matches '{ref}'. If this is a project "
                    "name rather than an organization, use "
                    "explain_project_credit_balance instead."
                ),
            }

        # Credits are customer-role scoped (Permissions expose only
        # customer_path) — scope through filter_queryset_for_user so project
        # membership alone cannot read the organization's credit posture.
        customer_credit = (
            filter_queryset_for_user(CustomerCredit.objects.all(), user)
            .filter(customer=customer)
            .first()
        )
        credits = list(
            filter_queryset_for_user(ProjectCredit.objects.all(), user)
            .filter(project__customer=customer)
            .select_related("project")
        )

        # Bulk-sum spend per project (avoid a per-project query). ``total`` is
        # a Python property, so group in memory rather than aggregating in SQL.
        project_ids = [credit.project_id for credit in credits]
        spent_by_project: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        items = filter_queryset_for_user(InvoiceItem.objects.all(), user).filter(
            project_id__in=project_ids
        )
        for item in items:
            spent_by_project[item.project_id] += item.total

        projects: list[dict] = []
        overdrawn_count = 0
        for credit in credits:
            spent = spent_by_project[credit.project_id]
            balance = credit.value - spent
            is_overdrawn = balance < 0
            if is_overdrawn:
                overdrawn_count += 1
            projects.append(
                {
                    "project_name": credit.project.name,
                    "project_uuid": str(credit.project.uuid),
                    "credit_value": str(credit.value),
                    "spent_to_date": str(spent),
                    "balance_remaining": str(balance),
                    "is_overdrawn": is_overdrawn,
                }
            )

        # Worst balance first (most negative → most overdrawn).
        projects.sort(key=lambda row: Decimal(row["balance_remaining"]))
        truncated = len(projects) > MAX_LIST_RESULTS
        rows = projects[:MAX_LIST_RESULTS]

        customer_credit_data: dict | None = None
        if customer_credit:
            customer_credit_data = {
                "value": str(customer_credit.value),
                "expected_consumption": str(customer_credit.expected_consumption),
                "allocated_to_projects": str(customer_credit.allocated_to_projects),
                "end_date": customer_credit.end_date.isoformat()
                if customer_credit.end_date
                else None,
            }

        envelope = (
            f"customer credit {customer_credit.value}"
            if customer_credit
            else "no customer credit configured"
        )
        project_count = len(projects)
        if overdrawn_count:
            worst = rows[0]
            summary = (
                f"{customer.name}: {envelope}; {project_count} project(s) with "
                f"credit, {overdrawn_count} overdrawn (worst: "
                f"{worst['project_name']} at {worst['balance_remaining']})."
            )
        else:
            summary = (
                f"{customer.name}: {envelope}; {project_count} project(s) with "
                f"credit, none overdrawn."
            )

        return {
            "type": "success",
            "data": {
                "customer": {"uuid": str(customer.uuid), "name": customer.name},
                "customer_credit": customer_credit_data,
                "projects": rows,
                "overdrawn_count": overdrawn_count,
                "_total_project_count": project_count,
                "_truncated": truncated,
            },
            "summary": summary,
        }


tool_registry.register(GetCustomerCreditOverviewTool())
