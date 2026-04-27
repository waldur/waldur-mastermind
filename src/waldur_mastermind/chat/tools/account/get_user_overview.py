"""AI Assistant tool: bundled snapshot of another user's state (support-only)."""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone

from waldur_mastermind.chat.tools.account.helpers import (
    user_accessible_customers,
    user_accessible_projects,
    user_role_on_customer,
    user_role_on_project,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.models import Order, Resource

logger = logging.getLogger(__name__)

_LIST_CAP = 50
_ERROR_CAP = 10
_RECENT_WINDOW_DAYS = 14


class GetUserOverviewTool(BaseTool):
    """Bundled single-call snapshot of a target user (support triage)."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_USER_OVERVIEW,
            category=ToolCategory.ACCOUNT,
            description=(
                "Support-only snapshot of another user's state: "
                "organizations, projects, resource counts, pending orders, "
                "and recent errors."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_email": {
                        "type": "string",
                        "description": "Target user's email address.",
                    },
                },
                "required": ["user_email"],
            },
            usage_instructions=(
                "Use as the starting point when a support user says "
                "'show me the state of user X'. Returns everything you "
                "usually need in one call — drill into nav tools only if "
                "the answer needs more detail."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        email = (arguments.get("user_email") or "").strip()
        if not email:
            return {
                "type": "validation_error",
                "summary": "user_email is required.",
            }

        User = get_user_model()
        target = User.objects.filter(email__iexact=email).first()
        if target is None:
            return {"type": "error", "summary": f"User not found: {email}."}

        customers_qs = user_accessible_customers(target).order_by("name")
        organizations = [
            {
                "uuid": str(c.uuid),
                "name": c.name,
                "role": (r.value if (r := user_role_on_customer(target, c)) else None),
            }
            for c in customers_qs[:_LIST_CAP]
        ]

        projects_qs = (
            user_accessible_projects(target).select_related("customer").order_by("name")
        )
        projects = [
            {
                "uuid": str(p.uuid),
                "name": p.name,
                "organization_name": p.customer.name if p.customer_id else "",
                "role": (r.value if (r := user_role_on_project(target, p)) else None),
            }
            for p in projects_qs[:_LIST_CAP]
        ]

        resources_qs = Resource.objects.all().filter_for_service_consumer(target)
        total_resources = resources_qs.count()
        state_labels = dict(ResourceStates.CHOICES)
        by_state_raw = resources_qs.values("state").annotate(count=Count("id"))
        by_state = {
            state_labels.get(row["state"], str(row["state"])): row["count"]
            for row in by_state_raw
        }

        erred_resources = list(
            resources_qs.filter(state=ResourceStates.ERRED).order_by("-modified")[
                :_ERROR_CAP
            ]
        )
        erred = [
            {
                "uuid": str(r.uuid),
                "name": r.name,
                "error_message": r.error_message or "",
            }
            for r in erred_resources
        ]

        pending_qs = (
            Order.objects.filter(
                created_by=target, state__in=OrderStates.PENDING_STATES
            )
            .select_related("offering", "project")
            .order_by("-created")[:_ERROR_CAP]
        )
        pending_orders = [
            {
                "uuid": str(o.uuid),
                "offering_name": o.offering.name if o.offering_id else "",
                "project_name": o.project.name if o.project_id else "",
                "created": o.created.isoformat() if o.created else None,
            }
            for o in pending_qs
        ]

        cutoff = timezone.now() - timedelta(days=_RECENT_WINDOW_DAYS)
        recent_erred_resources = resources_qs.filter(
            state=ResourceStates.ERRED, modified__gte=cutoff
        ).order_by("-modified")[:_ERROR_CAP]
        recent_failed_orders = Order.objects.filter(
            created_by=target, state=OrderStates.ERRED, modified__gte=cutoff
        ).order_by("-modified")[:_ERROR_CAP]

        recent_errors = [
            {
                "resource_name": r.name,
                "error_message": (r.error_message or "")[:500],
                "error_traceback_head": (r.error_traceback or "")[:500],
                "when": r.modified.isoformat() if r.modified else None,
            }
            for r in recent_erred_resources
        ] + [
            {
                "resource_name": o.offering.name if o.offering_id else "",
                "error_message": (o.error_message or "")[:500],
                "error_traceback_head": (o.error_traceback or "")[:500],
                "when": o.modified.isoformat() if o.modified else None,
            }
            for o in recent_failed_orders
        ]
        recent_errors = recent_errors[:_ERROR_CAP]

        return {
            "type": "success",
            "data": {
                "user": {
                    "uuid": str(target.uuid),
                    "full_name": target.full_name,
                    "email": target.email,
                    "is_active": target.is_active,
                    "last_login": (
                        target.last_login.isoformat() if target.last_login else None
                    ),
                },
                "organizations": organizations,
                "projects": projects,
                "resources": {
                    "total": total_resources,
                    "by_state": by_state,
                    "erred": erred,
                },
                "pending_orders": pending_orders,
                "recent_errors": recent_errors,
            },
            "summary": (
                f"Overview for {email}: {total_resources} resources, "
                f"{len(projects)} projects, "
                f"{by_state.get('Erred', 0)} erred."
            ),
        }


tool_registry.register(GetUserOverviewTool())
