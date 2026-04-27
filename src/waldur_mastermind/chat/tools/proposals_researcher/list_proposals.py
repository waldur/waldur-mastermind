import logging

from django.db.models import Q
from django.utils import timezone

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import validate_uuid
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.proposal_helpers import proposal_detail_url
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.models import Proposal

logger = logging.getLogger(__name__)

_MAX_PROPOSALS = 30
_VALID_PROPOSAL_STATES = {s for s, _ in ProposalStates.CHOICES}


class ListProposalsTool(BaseTool):
    """List proposals visible to the user, filterable by call, state, ownership, or text."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_PROPOSALS,
            category=ToolCategory.PROPOSALS_RESEARCHER,
            description=(
                "List proposals the current user can see, optionally filtered by "
                "call, proposal state, ownership, or free-text. Use for any "
                "aggregate proposal query — for one specific proposal use "
                "proposal_overview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Restrict to proposals on this call (UUID fresh "
                            "from this turn)."
                        ),
                    },
                    "call_name": {
                        "type": "string",
                        "description": (
                            "Restrict to proposals on this call (name typed by "
                            "user or carried from an earlier turn)."
                        ),
                    },
                    "state": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(_VALID_PROPOSAL_STATES),
                        },
                        "description": (
                            "Filter to proposals in any of these states. "
                            "Omit to include all states."
                        ),
                    },
                    "mine": {
                        "type": "boolean",
                        "description": (
                            "If true, restrict to proposals the current user "
                            "created. Default false."
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Free-text search over proposal name and "
                            "project_summary. Text only — never pass a UUID."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use when the user asks about proposals in aggregate:\n"
                "  ✓ 'Show my proposals' → mine=true\n"
                "  ✓ 'What proposals are in_review on call X?' → state=['in_review'], call_name='X'\n"
                "  ✓ 'List accepted proposals'\n"
                "  ✗ 'Tell me about proposal R1-001' — use proposal_overview.\n"
                "\n"
                "Permission scoping is automatic: applicants see their own + "
                "proposals on calls they're connected to; reviewers see "
                "assigned + connected; staff see all.\n"
                "\n"
                "Picking `call_uuid` vs `call_name`: fresh from this turn → "
                "uuid; earlier turn or typed by user → name. Prefer name in "
                "doubt. Never pass a UUID into `search`.\n"
                "\n"
                "ALWAYS render results as a markdown table with EXACTLY these "
                "columns and no others: Slug | Name | Call | State | Deadline "
                "| Action. The Action cell MUST be `[Open](url)` using each "
                "proposal's `url` field verbatim. Do NOT add a separate row of "
                "pill links above or below the table — the Action column is "
                "the only CTA."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        call_uuid = (arguments.get("call_uuid") or "").strip()
        call_name = (arguments.get("call_name") or "").strip()
        states = arguments.get("state") or []
        mine = bool(arguments.get("mine"))
        search = (arguments.get("search") or "").strip()

        if call_uuid and not validate_uuid(call_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for call_uuid: {call_uuid}",
            }

        invalid_states = [s for s in states if s not in _VALID_PROPOSAL_STATES]
        if invalid_states:
            return {
                "type": "validation_error",
                "summary": f"Invalid proposal state(s): {invalid_states}",
            }

        qs = filter_queryset_for_user(Proposal.objects.all(), user).select_related(
            "round__call", "created_by"
        )

        if call_uuid:
            qs = qs.filter(round__call__uuid=call_uuid)
        if call_name:
            qs = qs.filter(round__call__name__icontains=call_name)
        if states:
            qs = qs.filter(state__in=states)
        if mine:
            qs = qs.filter(created_by=user)
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(project_summary__icontains=search)
            )

        proposals = list(qs.order_by("-created")[:_MAX_PROPOSALS])

        now = timezone.now()
        data = []
        for p in proposals:
            call = p.round.call if p.round else None
            url = proposal_detail_url(str(p.uuid))
            data.append(
                {
                    "slug": p.slug,
                    "name": p.name,
                    "state": p.state,
                    "call_name": call.name if call else "",
                    "deadline": p.round.cutoff_time.isoformat()
                    if p.round and p.round.cutoff_time
                    else None,
                    "days_until_deadline": (p.round.cutoff_time - now).days
                    if p.round and p.round.cutoff_time
                    else None,
                    "created_by": p.created_by.full_name if p.created_by else "",
                    "url": url,
                }
            )

        summary = (
            f"{len(data)} proposal{'s' if len(data) != 1 else ''} matching your filter."
        )
        return {
            "type": "success",
            "data": {"proposals": data, "total": len(data)},
            "summary": summary,
        }


tool_registry.register(ListProposalsTool())
