import logging

from django.utils import timezone

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.proposal_helpers import call_detail_url
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.models import (
    Call,
    CallResourceTemplate,
    Proposal,
)

logger = logging.getLogger(__name__)

_MAX_CALLS = 20


class FindMatchingCallsTool(BaseTool):
    """Finds active calls matching a researcher's project description and resource needs."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.FIND_MATCHING_CALLS,
            description=(
                "Find open calls for proposals that match the user's research project. "
                "Returns active calls with their descriptions, deadlines, available resources, "
                "and submission statistics to help users discover the best call to apply to."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "research_description": {
                        "type": "string",
                        "description": (
                            "Description of the user's research project, "
                            "resource needs, and scientific domain."
                        ),
                    },
                },
                "required": ["research_description"],
            },
            route_utterances=[
                "I need GPU time for climate modeling, which calls should I apply to?",
                "Which open calls accept AI/ML proposals?",
                "Find calls matching my protein folding research",
                "I want to apply for HPC resources, what's available?",
                "Where should I submit my computational biology proposal?",
                "Are there any calls for quantum computing projects?",
                "What funding opportunities are open right now?",
            ],
            usage_instructions=(
                "Use when the user describes their research project and wants to find "
                "suitable calls to apply to:\n"
                "  ✓ 'I need GPU time for climate modeling'\n"
                "  ✓ 'Which calls accept AI/ML proposals?'\n"
                "  ✓ 'I want to run protein folding simulations, where should I apply?'\n"
                "  ✗ 'Show my proposals' — use different approach\n"
                "  ✗ 'What is a call?' — answer with knowledge, no tool needed"
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        research_description = arguments.get("research_description", "")
        now = timezone.now()

        active_calls = (
            Call.objects.filter(state=Call.States.ACTIVE)
            .select_related("manager__customer")
            .prefetch_related("round_set")[:_MAX_CALLS]
        )

        if not active_calls:
            return {
                "type": "success",
                "data": {"calls": [], "total": 0},
                "summary": "No active calls found at this time.",
                "ui_component": "markdown",
                "ui_data": {
                    "c": "There are currently no active calls for proposals. Check back later.",
                },
            }

        calls_data = []
        for call in active_calls:
            open_rounds = call.round_set.filter(
                start_time__lte=now, cutoff_time__gte=now
            )
            upcoming_rounds = call.round_set.filter(start_time__gt=now)

            nearest_deadline = None
            for r in open_rounds:
                if nearest_deadline is None or r.cutoff_time < nearest_deadline:
                    nearest_deadline = r.cutoff_time

            resource_templates = CallResourceTemplate.objects.filter(
                requested_offering__call=call
            ).select_related("requested_offering__offering")

            resources = []
            for tpl in resource_templates:
                offering_name = (
                    tpl.requested_offering.offering.name
                    if tpl.requested_offering and tpl.requested_offering.offering
                    else tpl.name
                )
                resources.append(
                    {
                        "name": tpl.name or offering_name,
                        "limits": tpl.limits if tpl.limits else {},
                    }
                )

            proposal_count = (
                Proposal.objects.filter(round__call=call)
                .exclude(state=Proposal.States.CANCELED)
                .count()
            )
            accepted_count = Proposal.objects.filter(
                round__call=call, state=Proposal.States.ACCEPTED
            ).count()

            calls_data.append(
                {
                    "name": call.name,
                    "uuid": str(call.uuid),
                    "url": call_detail_url(str(call.uuid)),
                    "description": call.description[:500] if call.description else "",
                    "organization": call.manager.customer.name if call.manager else "",
                    "open_rounds": open_rounds.count(),
                    "upcoming_rounds": upcoming_rounds.count(),
                    "nearest_deadline": nearest_deadline.isoformat()
                    if nearest_deadline
                    else None,
                    "days_until_deadline": (nearest_deadline - now).days
                    if nearest_deadline
                    else None,
                    "available_resources": resources,
                    "proposals_submitted": proposal_count,
                    "proposals_accepted": accepted_count,
                }
            )

        nav_links = [
            {"label": c["name"], "url": c["url"], "variant": "primary"}
            for c in calls_data
        ]

        summary = (
            f"Found {len(calls_data)} active call{'s' if len(calls_data) != 1 else ''} "
            f"for proposals. User research focus: {research_description[:100]}"
        )

        return {
            "type": "success",
            "data": {
                "calls": calls_data,
                "research_description": research_description,
                "total": len(calls_data),
            },
            "summary": summary,
            "ui_component": "homeport_nav",
            "ui_data": {
                "links": nav_links,
                "content": summary,
            },
        }


tool_registry.register(FindMatchingCallsTool())
