"""AI Assistant tool: explain credit compensations applied to an invoice.

Answers the recurring corner-case support question "why does my invoice
show 0 / why was my project compensated / are there hidden adjustments?"

The data model:
  - Original chargeable items: ``credit_uuid IS NULL``, positive unit_price.
  - Credit compensations: ``credit_uuid IS NOT NULL``, negative unit_price,
    name prefix ``"Credit compensation. ..."`` — concealed from end-user
    invoice views via ``utils.filter_invoice_items(conceal_compensation_items=True)``.
  - Manual cost adjustments / refunds: ``credit_uuid IS NULL``, negative
    unit_price — visible to end users.

The tool reports gross / compensation / net plus a per-resource
breakdown that flags resources concealed from end users (state ==
TERMINATED) so support can see what end users can't.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import (
    user_accessible_projects,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace.enums import ResourceStates

logger = logging.getLogger(__name__)


def _is_compensation(item: InvoiceItem) -> bool:
    return item.credit_id is not None


def _is_manual_refund(item: InvoiceItem) -> bool:
    return item.credit_id is None and item.total < 0


class ExplainInvoiceCompensationsTool(BaseTool):
    """Explain compensations + adjustments applied to a project's invoice."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.EXPLAIN_INVOICE_COMPENSATIONS,
            category=ToolCategory.ACCOUNT,
            description=(
                "Explain credit compensations and manual cost adjustments "
                "applied to a project's invoice. Distinguishes credit-"
                "compensated rows (paid out of customer/project credit, "
                "hidden from end-user views) from manual refunds (no "
                "credit FK). Surfaces resources concealed from end users "
                "(terminated state) that still drove invoice activity. "
                "Accepts project_uuid OR project_name; year+month default "
                "to the latest invoice."
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
                    "year": {
                        "type": "integer",
                        "description": "Optional invoice year (default: latest).",
                    },
                    "month": {
                        "type": "integer",
                        "description": "Optional invoice month 1-12 (default: latest).",
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use when the user asks 'why does my invoice show 0?', "
                "'how was my project compensated?', 'are there hidden "
                "adjustments on this invoice?', or 'why are credits "
                "still being drained on a terminated resource?'. ALWAYS "
                "use this for compensation / refund / hidden-adjustment "
                "questions — get_resource_usage and explain_project_credit_"
                "balance won't surface the compensation rows."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        project_uuid = (arguments.get("project_uuid") or "").strip()
        project_name = (arguments.get("project_name") or "").strip()
        year = arguments.get("year")
        month = arguments.get("month")

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

        # InvoiceItem is customer-role scoped (Permissions exposes only
        # customer_path); scope through filter_queryset_for_user so project
        # membership alone cannot read customer billing — staff/support see all.
        items_qs = (
            filter_queryset_for_user(InvoiceItem.objects.all(), user)
            .filter(project=project)
            .select_related("invoice", "resource", "credit")
        )
        if year and month:
            items_qs = items_qs.filter(invoice__year=year, invoice__month=month)
        else:
            latest = (
                items_qs.order_by("-invoice__year", "-invoice__month")
                .values("invoice__year", "invoice__month")
                .first()
            )
            if latest is None:
                return {
                    "type": "success",
                    "data": {
                        "project": {"uuid": str(project.uuid), "name": project.name},
                        "invoice_summary": None,
                        "per_resource": [],
                        "manual_adjustments": [],
                    },
                    "summary": (f"{project.name} has no invoice items in any month."),
                }
            year, month = latest["invoice__year"], latest["invoice__month"]
            items_qs = items_qs.filter(invoice__year=year, invoice__month=month)

        items = list(items_qs)

        gross = Decimal("0")
        compensation_total = Decimal("0")
        manual_refunds_total = Decimal("0")
        from_customer_credit = Decimal("0")
        manual_adjustments: list[dict] = []
        per_resource: dict[str, dict] = {}

        for it in items:
            row_total = it.total
            res = it.resource
            if res is None:
                # Items detached from a resource (e.g. minimal_consumption tail
                # rows) — bucket separately.
                if _is_compensation(it):
                    compensation_total += -row_total  # negative → positive comp value
                elif _is_manual_refund(it):
                    manual_refunds_total += -row_total
                    manual_adjustments.append(
                        {
                            "name": it.name,
                            "amount": str(row_total),
                            "invoice_year": it.invoice.year,
                            "invoice_month": it.invoice.month,
                        }
                    )
                continue

            row = per_resource.setdefault(
                str(res.uuid),
                {
                    "resource_uuid": str(res.uuid),
                    "resource_name": res.name,
                    "state": dict(ResourceStates.CHOICES).get(res.state, res.state),
                    "hidden_from_user": res.state == ResourceStates.TERMINATED,
                    "gross": Decimal("0"),
                    "compensation": Decimal("0"),
                    "compensation_source": None,
                },
            )
            if _is_compensation(it):
                comp_value = -row_total
                row["compensation"] += comp_value
                compensation_total += comp_value
                # Heuristic source attribution: prod stores credit FK to
                # CustomerCredit, but project credit is decremented in
                # parallel. We surface it as customer_credit because that's
                # what the FK points to; tail rows show in a dedicated bucket.
                row["compensation_source"] = "customer_credit"
                from_customer_credit += comp_value
            elif _is_manual_refund(it):
                manual_refunds_total += -row_total
                manual_adjustments.append(
                    {
                        "name": it.name,
                        "amount": str(row_total),
                        "resource_name": res.name,
                        "invoice_year": it.invoice.year,
                        "invoice_month": it.invoice.month,
                    }
                )
            else:
                row["gross"] += row_total
                gross += row_total

        net_charged = gross - compensation_total - manual_refunds_total

        # Stringify Decimals so JSON encoding is stable across consumers.
        per_resource_list = []
        for r in per_resource.values():
            per_resource_list.append(
                {
                    **r,
                    "gross": str(r["gross"]),
                    "compensation": str(r["compensation"]),
                    "net": str(r["gross"] - r["compensation"]),
                }
            )
        per_resource_list.sort(
            key=lambda r: (Decimal(r["compensation"]), Decimal(r["gross"])),
            reverse=True,
        )

        hidden_count = sum(1 for r in per_resource_list if r["hidden_from_user"])
        summary_pieces = [
            f"{project.name} invoice {year}-{month:02d}: ",
            f"gross {gross}, ",
        ]
        if compensation_total > 0:
            summary_pieces.append(
                f"credit compensation {compensation_total} (drained from customer credit), "
            )
        if manual_refunds_total > 0:
            summary_pieces.append(f"manual adjustments {manual_refunds_total}, ")
        summary_pieces.append(f"net charged to customer {net_charged}.")
        if hidden_count:
            summary_pieces.append(
                f" {hidden_count} resource(s) concealed from end-user views "
                f"(state=Terminated) but still drove invoice activity."
            )
        summary = "".join(summary_pieces)

        return {
            "type": "success",
            "data": {
                "project": {"uuid": str(project.uuid), "name": project.name},
                "invoice_summary": {
                    "year": year,
                    "month": month,
                    "gross": str(gross),
                    "compensation_total": str(compensation_total),
                    "manual_refunds_total": str(manual_refunds_total),
                    "net_charged_to_customer": str(net_charged),
                    "from_customer_credit": str(from_customer_credit),
                },
                "per_resource": per_resource_list,
                "manual_adjustments": manual_adjustments,
                "concealed_resource_count": hidden_count,
            },
            "summary": summary,
        }


tool_registry.register(ExplainInvoiceCompensationsTool())
