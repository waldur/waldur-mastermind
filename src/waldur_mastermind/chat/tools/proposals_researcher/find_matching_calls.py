import logging

from django.utils import timezone

from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.proposal_helpers import call_detail_url
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.models import (
    Call,
    CallResourceTemplate,
    Proposal,
)

logger = logging.getLogger(__name__)


class FindMatchingCallsTool(BaseTool):
    """Finds active calls matching a researcher's project description and resource needs."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.FIND_MATCHING_CALLS,
            category=ToolCategory.PROPOSALS_RESEARCHER,
            description=(
                "Find open calls for proposals that match the user's research project. "
                "Returns active calls with their descriptions, deadlines, available resources, "
                "and submission statistics to help users discover the best call to apply to.\n"
                "\n"
                "ALWAYS render results as a markdown table with one row per "
                "call and a final `Action` column whose cell is `[Open](url)`. "
                "Use each call's `url` field verbatim. Do NOT add a separate "
                "row of pill links above or below the table — the Action "
                "column is the only CTA."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Short research keywords (domain, resource type, technique). "
                            "Do NOT pass a full sentence or description. "
                            "Examples: ['GPU', 'climate modeling'], "
                            "['drug discovery', 'CUDA', 'A100']."
                        ),
                    },
                },
                "required": ["keywords"],
            },
            usage_instructions=(
                "GATE: If the user has not provided a research domain, project description, "
                "or specific resource need — only vague phrasing like 'recommend me calls', "
                "'find me proposals', 'which calls should I apply to' — call ask_user FIRST "
                "to collect their domain and resource needs, then call this tool with the "
                "resulting keywords. Do not fabricate generic placeholder keywords ('research', "
                "'science') from a vague request.\n"
                "\n"
                "Use when the user describes their research project and wants to find "
                "suitable calls to apply to. Extract 2-5 keywords from what the user "
                "said — do not paraphrase into prose:\n"
                "  ✓ user: 'I need GPU time for climate modeling' → ['GPU', 'climate modeling']\n"
                "  ✓ user: 'Which calls accept AI/ML proposals?' → ['AI', 'ML']\n"
                "  ✓ user: 'protein folding simulations' → ['protein folding', 'simulation']\n"
                "  ✗ 'Show my proposals' — use list_proposals instead\n"
                "  ✗ 'What calls are open?' — use list_calls (no research keywords)\n"
                "  ✗ 'What is a call?' — answer with knowledge, no tool needed\n"
                "\n"
                "ALWAYS render results as a markdown table with EXACTLY these "
                "columns and no others: Call Name | Organization | Open Rounds "
                "| Deadline | Action. The Action cell MUST be `[Open](url)` "
                "using each call's `url` field verbatim. Do NOT add a separate "
                "row of pill links above or below the table — the Action column "
                "is the only CTA."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        raw_keywords = arguments.get("keywords", [])
        if isinstance(raw_keywords, str):
            raw_keywords = [k.strip() for k in raw_keywords.split(",")]
        keywords = [k for k in (kw.strip() for kw in raw_keywords if kw) if k]
        now = timezone.now()

        active_qs = (
            Call.objects.filter(state=Call.States.ACTIVE)
            .select_related("manager__customer")
            .prefetch_related("round_set")
        )
        total_count = active_qs.count()
        active_calls = list(active_qs[:MAX_LIST_RESULTS])
        truncated = total_count > MAX_LIST_RESULTS

        if not active_calls:
            return {
                "type": "success",
                "data": {
                    "calls": [],
                    "total": 0,
                    "_total_count": 0,
                    "_truncated": False,
                    "keywords": keywords,
                },
                "summary": "No active calls found at this time.",
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

        keywords_label = ", ".join(keywords) if keywords else "no keywords"
        summary = (
            f"Found {len(calls_data)} active call"
            f"{'s' if len(calls_data) != 1 else ''} for proposals. "
            f"Keywords: {keywords_label}."
        )
        if truncated:
            summary += (
                f" Showing first {MAX_LIST_RESULTS} of {total_count} active "
                "calls — narrow keywords to see more."
            )

        return {
            "type": "success",
            "data": {
                "calls": calls_data,
                "keywords": keywords,
                "total": len(calls_data),
                "_total_count": total_count,
                "_truncated": truncated,
            },
            "summary": summary,
        }


tool_registry.register(FindMatchingCallsTool())
