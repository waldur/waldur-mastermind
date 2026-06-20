"""AI Assistant tool: explain why a resource is in paused state.

Combines three independent mechanisms that can leave a resource
unavailable, in priority order:

  1. Cost-policy pausing (ProjectEstimatedCostPolicy fired,
     limit_cost exceeded by project spend).
  2. SLURM grace-ratio pausing (SlurmPeriodicPolicy: usage% over
     (1+grace_ratio)*100).
  3. Project end-date / grace-period expiry (independent of policy
     machinery — included as compounding context even when 1 or 2 is
     the primary cause).

Reads the structured ``Resource.attributes._policy_attribution.paused``
blob the production policy code writes via
``policy/policy_actions.py::_save_resource_with_reversion``.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import (
    sum_invoice_item_totals,
    user_accessible_projects,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

logger = logging.getLogger(__name__)


def _project_grace_context(project) -> dict | None:
    """Return end-date / grace-period info for a project, or None."""
    if not project or not project.end_date:
        return None
    return {
        "end_date": project.end_date.isoformat(),
        "is_expired": bool(project.is_expired),
        "is_in_grace_period": bool(project.is_in_grace_period),
        "effective_end_date": (
            project.end_date_with_grace.isoformat()
            if project.end_date_with_grace
            else None
        ),
    }


class ExplainResourcePausedReasonTool(BaseTool):
    """Explain why a resource is paused (cost policy, SLURM grace, end-date)."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.EXPLAIN_RESOURCE_PAUSED_REASON,
            category=ToolCategory.ACCOUNT,
            description=(
                "Authoritative tool for any question about a resource's "
                "paused state — reports the primary cause (cost policy "
                "fired, SLURM grace ratio exceeded, manual pause, or "
                "'not paused') along with supporting numbers (limit cost "
                "vs spend; usage % vs grace limit) and the project's "
                "end-date / grace-period context. ALWAYS use this for "
                "'is X paused?' / 'why is X paused?' / 'when was X "
                "paused?' — never use get_project_resources for a "
                "paused-status check, it doesn't surface the cause. "
                "Accepts resource_uuid or resource_name."
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
                            "Resource name (icontains) when no UUID is known."
                        ),
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Optional project name to disambiguate when several "
                            "resources share the same name across projects."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use when the user asks 'why is resource X paused?', "
                "'when did resource X get paused?', or 'is resource X "
                "available?'. Always cite the primary cause AND the "
                "project-end-date context — both can compound."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        resource_uuid = (arguments.get("resource_uuid") or "").strip()
        resource_name = (arguments.get("resource_name") or "").strip()
        project_name = (arguments.get("project_name") or "").strip()

        if not resource_uuid and not resource_name:
            return {
                "type": "validation_error",
                "summary": "Pass resource_uuid or resource_name.",
            }
        if resource_uuid and not validate_uuid(resource_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for resource_uuid: {resource_uuid}",
            }

        # Visible projects gate: a resource is reachable only if its
        # project is reachable to the caller.
        accessible_projects = user_accessible_projects(user)
        qs = Resource.objects.filter(project__in=accessible_projects).select_related(
            "project", "project__customer"
        )
        if resource_uuid:
            resource = qs.filter(uuid=resource_uuid).first()
        else:
            qs = qs.filter(name__icontains=resource_name)
            if project_name:
                qs = qs.filter(project__name__icontains=project_name)
            resource = qs.first()
        if resource is None:
            return {"type": "error", "summary": "Resource not found."}

        project = resource.project
        state_labels = dict(ResourceStates.CHOICES)
        grace_context = _project_grace_context(project)

        # Mechanism C — end-date expiry. Surfaced regardless of paused
        # status because it can independently make a resource unusable.
        end_date_alone = (
            grace_context is not None
            and grace_context["is_expired"]
            and not resource.paused
        )

        # Resource not paused → return early with the project end-date
        # context anyway (a user asking "why is X paused" might be wrong
        # about the premise, in which case stating it cleanly matters).
        if not resource.paused:
            primary_cause = "project_end_date" if end_date_alone else "not_paused"
            summary = (
                f"{resource.name} is NOT paused (state="
                f"{state_labels.get(resource.state, resource.state)})."
            )
            if end_date_alone:
                summary += (
                    f" Note: project '{project.name}' has end_date "
                    f"{grace_context['end_date']} which is past — "
                    f"resource is likely unavailable for this reason."
                )
            return {
                "type": "success",
                "data": {
                    "resource": {
                        "uuid": str(resource.uuid),
                        "name": resource.name,
                        "paused": False,
                        "downscaled": resource.downscaled,
                        "restrict_member_access": resource.restrict_member_access,
                        "state": state_labels.get(resource.state, resource.state),
                    },
                    "project": {
                        "name": project.name,
                        "uuid": str(project.uuid),
                        **(grace_context or {}),
                    },
                    "primary_cause": primary_cause,
                    "attribution": None,
                    "policy_details": None,
                },
                "summary": summary,
            }

        # Resource IS paused — read attribution blob.
        attribution = None
        if isinstance(resource.attributes, dict):
            attribution = resource.attributes.get("_policy_attribution", {}).get(
                "paused"
            )
        primary_cause = "manual"
        policy_details: dict = {}

        if attribution and attribution.get("policy_class"):
            klass = attribution.get("policy_class", "")
            if klass == "ProjectEstimatedCostPolicy":
                primary_cause = "cost_policy"
                policy_details["policy_kind"] = klass
                policy_details["limit_cost"] = attribution.get("limit_cost")
                # Compare current spend vs limit so the LLM has fresh
                # numbers, not the snapshot at pause time.
                # Customer-role scoped: project membership alone must not
                # surface customer billing totals (staff/support see all).
                spent_to_date = sum_invoice_item_totals(
                    filter_queryset_for_user(InvoiceItem.objects.all(), user).filter(
                        project=project
                    )
                )
                policy_details["current_spend"] = str(spent_to_date)
                try:
                    limit = Decimal(attribution.get("limit_cost") or "0")
                    policy_details["exceeded_by"] = str(
                        max(Decimal("0"), spent_to_date - limit)
                    )
                except (ValueError, TypeError):
                    policy_details["exceeded_by"] = None
            elif klass == "SlurmPeriodicPolicy":
                primary_cause = "slurm_grace"
                policy_details["policy_kind"] = klass
                policy_details["grace_ratio"] = attribution.get("grace_ratio")
                # The exact "Usage: X%" value is in the reversion comment;
                # we don't read django-reversion here to keep the tool
                # lightweight. The attribution timestamp is enough for
                # the chat-flow narrative.
            else:
                policy_details["policy_kind"] = klass

        # Build summary + return
        timestamp_part = ""
        if attribution and attribution.get("timestamp"):
            timestamp_part = f" on {attribution['timestamp'][:10]}"
        if primary_cause == "cost_policy":
            summary = (
                f"{resource.name} is paused{timestamp_part} by cost policy "
                f"(limit {policy_details.get('limit_cost')}, current spend "
                f"{policy_details.get('current_spend')}, exceeded by "
                f"{policy_details.get('exceeded_by')})."
            )
        elif primary_cause == "slurm_grace":
            summary = (
                f"{resource.name} is paused{timestamp_part} by SLURM grace-ratio "
                f"policy (grace_ratio={policy_details.get('grace_ratio')})."
            )
        elif primary_cause == "manual":
            summary = (
                f"{resource.name} is paused but no policy attribution is "
                "recorded — likely a manual pause via the API/admin."
            )
        else:
            summary = f"{resource.name} is paused (cause={primary_cause})."

        if grace_context:
            if grace_context["is_in_grace_period"]:
                summary += (
                    f" Project '{project.name}' is also in grace period "
                    f"(end_date {grace_context['end_date']}, "
                    f"effective_end_date {grace_context['effective_end_date']})."
                )
            elif grace_context["is_expired"]:
                summary += (
                    f" Project '{project.name}' is past end_date "
                    f"({grace_context['end_date']}) — compounds the pause."
                )

        return {
            "type": "success",
            "data": {
                "resource": {
                    "uuid": str(resource.uuid),
                    "name": resource.name,
                    "paused": True,
                    "downscaled": resource.downscaled,
                    "restrict_member_access": resource.restrict_member_access,
                    "state": state_labels.get(resource.state, resource.state),
                },
                "project": {
                    "name": project.name,
                    "uuid": str(project.uuid),
                    **(grace_context or {}),
                },
                "primary_cause": primary_cause,
                "attribution": attribution,
                "policy_details": policy_details or None,
            },
            "summary": summary,
        }


tool_registry.register(ExplainResourcePausedReasonTool())
